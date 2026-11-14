"""Readiness barrier for native Zigbee2MQTT group commands (core only)."""

from __future__ import annotations

import asyncio
from itertools import count

from .models import (
    DesiredState,
    GroupAction,
    GroupDecision,
    GroupSnapshot,
    IntentPatch,
    Phase,
    SendStatus,
)
from .protocols import Clock
from .reconciler import GroupCircuit, Reconciler

_INDIVIDUAL = GroupDecision(GroupAction.INDIVIDUAL)


class _Operation:
    """One group ON request; it is a reservation, never a held resource lock."""

    def __init__(self, group: GroupCircuit, started_at: float) -> None:
        self.group = group
        self.started_at = started_at
        self.expected: dict[str, int] = {}
        self.arrived: dict[str, tuple[bool, asyncio.Future[GroupDecision]]] = {}
        self.event = asyncio.Event()
        self.final: GroupDecision | None = None
        self.task: asyncio.Task[None] | None = None

    def begin(self, light_id: str, generation: int) -> None:
        self.expected[light_id] = generation

    async def arrive(
        self, light_id: str, generation: int, needs_set: bool
    ) -> GroupDecision:
        if self.final is not None or self.expected.get(light_id) != generation:
            return self.final or _INDIVIDUAL
        future: asyncio.Future[GroupDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self.arrived[light_id] = (needs_set, future)
        self.event.set()
        try:
            return await future
        except asyncio.CancelledError:
            if self.arrived.get(light_id, (None, None))[1] is future:
                del self.arrived[light_id]
            self.event.set()
            raise

    def resolve(self, decision: GroupDecision) -> None:
        self.final = decision
        for _, future in self.arrived.values():
            if not future.done():
                future.set_result(decision)


class GroupCoordinator:
    """Send one native group command only when every member is current and ready.

    The reconciler keeps owning power and readiness of each circuit. While an
    operation is valid its members hand over only the final LED_SET decision;
    OFF or any new member intent invalidates the operation immediately.
    """

    def __init__(
        self,
        engine: Reconciler,
        clock: Clock,
        *,
        barrier_ms: int,
        members: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        if type(barrier_ms) is not int or barrier_ms <= 0:
            raise ValueError("barrier_ms must be a positive integer")
        self._engine = engine
        self._clock = clock
        self._barrier = barrier_ms / 1000
        self._members = dict(members or {})
        self._operations: set[_Operation] = set()
        self._sequence = count()
        self._closed = False
        self._unsubscribe = engine.subscribe(self._changed)

    def _changed(self, _light_ids: frozenset[str]) -> None:
        for operation in tuple(self._operations):
            operation.event.set()

    def submit(
        self, group_id: str, patch: IntentPatch, *, source: str
    ) -> dict[str, DesiredState]:
        if self._closed:
            raise RuntimeError("group coordinator closed")
        group = self._engine.groups.get(group_id)
        if group is None:
            # Desired state still updates. Without an exact native group the
            # members are commanded individually, never through a foreign group.
            return self._engine.submit(self._members[group_id], patch, source=source)
        members = group.members
        all_on = patch.on is True or (
            patch.on is None
            and all(self._engine.snapshot(item).desired.on for item in members)
        )
        if not all_on:
            return self._engine.submit(members, patch, source=source)
        operation = _Operation(group, self._clock.now())
        result = self._engine.submit(members, patch, source=source, gate=operation)
        if set(operation.expected) != set(members):
            # A group command would also reach a member this operation did not set.
            self._engine.release_gate(operation)
            return result
        self._operations.add(operation)
        operation.task = asyncio.create_task(self._run(operation))
        operation.task.add_done_callback(lambda _task: self._finish(operation))
        return result

    def _finish(self, operation: _Operation) -> None:
        self._operations.discard(operation)
        self._engine.release_gate(operation)

    def snapshot(self, group_id: str) -> GroupSnapshot:
        members = self._engine.groups[group_id].members
        snapshots = [self._engine.snapshot(item) for item in members]
        confirmed = sum(1 for item in snapshots if item.confirmed)
        return GroupSnapshot(
            on=any(item.desired.on for item in snapshots),
            confirmed_members=confirmed,
            member_count=len(members),
            confirmed=confirmed == len(members),
            available=all(item.available for item in snapshots),
        )

    def _is_valid(self, operation: _Operation) -> bool:
        return all(
            (state := self._engine.snapshot(item)).desired.generation
            == operation.expected[item]
            and state.desired.on
            and state.available
            for item in operation.group.members
        )

    async def _wait(self, operation: _Operation, timeout: float) -> None:
        wake = asyncio.create_task(operation.event.wait())
        timer = asyncio.create_task(self._clock.sleep(timeout))
        try:
            await asyncio.wait((wake, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wake, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake, timer, return_exceptions=True)

    async def _run(self, operation: _Operation) -> None:
        try:
            # Let the member workers take their first step before judging phases.
            await asyncio.sleep(0)
            deadline = operation.started_at + self._barrier
            group = operation.group
            while True:
                operation.event.clear()
                if not self._is_valid(operation) or any(
                    self._engine.snapshot(item).phase == Phase.ERROR
                    for item in group.members
                ):
                    operation.resolve(_INDIVIDUAL)
                    return
                if len(operation.arrived) == len(group.members):
                    await self._send(operation)
                    return
                remaining = deadline - self._clock.now()
                if remaining <= 0:
                    for item in group.members:
                        if item not in operation.arrived:
                            self._engine.abort_member(
                                item, operation.expected[item], "group_barrier_timeout"
                            )
                    operation.resolve(_INDIVIDUAL)
                    return
                await self._wait(operation, remaining)
        except asyncio.CancelledError:
            operation.resolve(GroupDecision(GroupAction.ABORT, reason="closed"))
            raise
        except Exception:  # noqa: BLE001 - fail closed on an unexpected error
            operation.resolve(GroupDecision(GroupAction.ABORT, reason="internal_error"))

    async def _send(self, operation: _Operation) -> None:
        group = operation.group
        if not any(needs for needs, _ in operation.arrived.values()):
            operation.resolve(_INDIVIDUAL)
            return
        payloads = {
            repr(sorted(self._engine.led_payload(item).items()))
            for item in group.members
        }
        if len(payloads) != 1:
            operation.resolve(_INDIVIDUAL)
            return
        payload = self._engine.led_payload(group.members[0])
        send = self._engine.send_group(
            group.group_id, payload, f"{group.group_id}-{next(self._sequence)}"
        )
        result = await asyncio.shield(send)
        if result.status == SendStatus.SENT:
            operation.resolve(GroupDecision(GroupAction.GROUP_SENT, self._clock.now()))
        elif result.status == SendStatus.STALE:
            operation.resolve(_INDIVIDUAL)
        else:
            operation.resolve(
                GroupDecision(GroupAction.ABORT, reason=result.reason or "group_failed")
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        tasks = [op.task for op in self._operations if op.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._operations.clear()
