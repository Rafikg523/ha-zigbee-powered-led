"""Schedule current MQTT commands without queuing historical targets."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from weakref import WeakKeyDictionary, WeakSet

from .models import CommandKind, CommandPriority, SendRequest, SendResult, SendStatus
from .protocols import Clock, Transport

# The process-wide registry prevents reload from bypassing an unfinished send.
# Resource identifiers must denote stable physical resources across config reloads.
_PROCESS_BUSY: set[str] = set()
_PROCESS_QUARANTINED: set[str] = set()
_INSTANCES: WeakSet[CommandArbiter] = WeakSet()
_PROCESS_OWNER: CommandArbiter | None = None
_LAST_PUBLISH: WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[float, float]] = (
    WeakKeyDictionary()
)


@dataclass(slots=True, eq=False)
class _Pending:
    request: SendRequest
    sequence: int
    due: float
    result: asyncio.Future[SendResult]


class CommandArbiter:
    """Serialize overlapping sends, giving queued OFF requests first choice.

    ``is_current`` must validate tokens, control mode, topology and connection
    generation. The arbiter invokes it again immediately before transport entry.
    Resource IDs must be canonical physical IDs stable across reloads. All arbiter
    instances in this process share resource reservations on one asyncio loop.
    A full process restart cannot retain an in-flight publication or quarantine.
    """

    def __init__(
        self,
        transport: Transport,
        clock: Clock,
        *,
        is_current: Callable[[SendRequest], bool],
        min_send_interval_ms: int,
        publication_timeout_ms: int,
        slider_coalesce_ms: int,
    ) -> None:
        for name, value in (
            ("min_send_interval_ms", min_send_interval_ms),
            ("publication_timeout_ms", publication_timeout_ms),
            ("slider_coalesce_ms", slider_coalesce_ms),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._transport = transport
        self._clock = clock
        self._is_current = is_current
        self._interval = min_send_interval_ms / 1000
        self._timeout = publication_timeout_ms / 1000
        self._coalesce = slider_coalesce_ms / 1000
        self._counter = count()
        self._pending: list[_Pending] = []
        self._busy = _PROCESS_BUSY
        self._quarantined = _PROCESS_QUARANTINED
        self._unsettled: set[asyncio.Task[bool]] = set()
        self._finalizing: set[_Pending] = set()
        self._running: dict[_Pending, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        _INSTANCES.add(self)

    def _notify(self) -> None:
        """Wake all instances when process-wide resource ownership changes."""
        for instance in tuple(_INSTANCES):
            instance._wake.set()

    def _valid(self, request: SendRequest) -> bool:
        """Fail closed if current configuration cannot be verified."""
        try:
            return self._is_current(request) is True
        except Exception:  # noqa: BLE001 - uncertainty must block a physical command
            return False

    async def send(self, request: SendRequest) -> SendResult:
        """Submit one command; queued obsolete settings are discarded."""
        global _PROCESS_OWNER
        if self._closed:
            return SendResult(SendStatus.BLOCKED, "closed")
        if _PROCESS_OWNER is not None and _PROCESS_OWNER is not self:
            return SendResult(SendStatus.BLOCKED, "arbiter_already_active")
        if not request.resources or len(set(request.resources)) != len(
            request.resources
        ):
            return SendResult(SendStatus.BLOCKED, "invalid_resources")
        if self._quarantined.intersection(request.resources):
            return SendResult(SendStatus.BLOCKED, "delivery_unknown")
        if not self._valid(request):
            return SendResult(SendStatus.STALE, "stale")
        if (
            request.kind == CommandKind.RELAY_OFF
            and request.priority != CommandPriority.OFF
        ):
            return SendResult(SendStatus.BLOCKED, "invalid_priority")
        if request.kind == CommandKind.RELAY_ON:
            for existing in (*self._pending, *self._running):
                other = existing.request
                if (
                    other.kind == request.kind
                    and other.topic == request.topic
                    and other.payload == request.payload
                    and other.tokens == request.tokens
                    and set(other.resources) == set(request.resources)
                    and other.topology_version == request.topology_version
                ):
                    return SendResult(SendStatus.BLOCKED, "duplicate")
        _PROCESS_OWNER = self
        future = asyncio.get_running_loop().create_future()
        is_setting = request.kind in (CommandKind.LED_SET, CommandKind.GROUP_SET)
        item = _Pending(
            request,
            next(self._counter),
            self._clock.now() + (self._coalesce if is_setting else 0),
            future,
        )
        resources = set(request.resources)
        for old in tuple(self._pending):
            overlaps = bool(resources.intersection(old.request.resources))
            superseded = request.kind == CommandKind.RELAY_OFF or (
                is_setting
                and old.request.kind in (CommandKind.LED_SET, CommandKind.GROUP_SET)
            )
            if overlaps and superseded:
                self._pending.remove(old)
                if not old.result.done():
                    old.result.set_result(SendResult(SendStatus.STALE, "superseded"))
        self._pending.append(item)
        if self._worker is None:
            self._worker = asyncio.create_task(self._schedule())
        self._notify()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if item in self._pending:
                self._pending.remove(item)
                self._notify()
            elif item in self._running and item not in self._finalizing:
                self._running[item].cancel()
            raise

    async def close(self) -> None:
        """Stop pending work; a cancelled in-flight publish has unknown outcome."""
        global _PROCESS_OWNER
        if self._closed:
            return
        self._closed = True
        for item in self._pending:
            if not item.result.done():
                item.result.set_result(SendResult(SendStatus.BLOCKED, "closed"))
        self._pending.clear()
        if self._worker:
            self._worker.cancel()
        for item, task in tuple(self._running.items()):
            if item not in self._finalizing:
                task.cancel()
        tasks = ([self._worker] if self._worker else []) + list(self._running.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if _PROCESS_OWNER is self:
            _PROCESS_OWNER = None

    async def _wait(self, delay: float | None) -> None:
        event = asyncio.create_task(self._wake.wait())
        timer = (
            asyncio.create_task(self._clock.sleep(delay)) if delay is not None else None
        )
        tasks = [event] + ([timer] if timer else [])
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _schedule(self) -> None:
        while not self._closed:
            self._wake.clear()
            for item in tuple(self._pending):
                if self._quarantined.intersection(item.request.resources):
                    self._pending.remove(item)
                    if not item.result.done():
                        item.result.set_result(
                            SendResult(SendStatus.BLOCKED, "delivery_unknown")
                        )
                elif not self._valid(item.request):
                    self._pending.remove(item)
                    if not item.result.done():
                        item.result.set_result(SendResult(SendStatus.STALE, "stale"))
            free = [
                item
                for item in self._pending
                if not self._busy.intersection(item.request.resources)
            ]
            now = self._clock.now()
            previous = _LAST_PUBLISH.get(asyncio.get_running_loop())
            rate_due = (
                now
                if previous is None
                else previous[0] + max(previous[1], self._interval)
            )
            ready = [item for item in free if item.due <= now and rate_due <= now]
            if ready:
                item = min(ready, key=lambda i: (i.request.priority, i.sequence))
                self._pending.remove(item)
                self._busy.update(item.request.resources)
                started = asyncio.Event()
                self._running[item] = asyncio.create_task(self._perform(item, started))
                self._running[item].add_done_callback(
                    lambda _task, pending=item, gate=started: self._recover_cancelled(
                        pending, gate
                    )
                )
                await started.wait()
                continue
            delay = min((max(item.due, rate_due) - now for item in free), default=None)
            await self._wait(max(delay, 0) if delay is not None else None)

    def _recover_cancelled(self, item: _Pending, started: asyncio.Event) -> None:
        """Release resources even if a task is cancelled before its first turn."""
        started.set()
        if item not in self._running:
            return
        self._running.pop(item, None)
        self._busy.difference_update(item.request.resources)
        if not item.result.done():
            item.result.set_result(SendResult(SendStatus.FAILED, "delivery_unknown"))
        self._notify()

    def _release_quarantine(
        self, task: asyncio.Task[bool], resources: tuple[str, ...]
    ) -> None:
        """Consume eventual completion and free only the settled resources."""
        if not task.cancelled():
            task.exception()
        self._unsettled.discard(task)
        self._quarantined.difference_update(resources)
        self._busy.difference_update(resources)
        self._notify()

    async def _perform(self, item: _Pending, started: asyncio.Event) -> None:
        publication: asyncio.Task[bool] | None = None
        timer: asyncio.Task[None] | None = None
        try:

            async def publish() -> bool:
                if self._closed or not self._valid(item.request):
                    started.set()
                    return False
                _LAST_PUBLISH[asyncio.get_running_loop()] = (
                    self._clock.now(),
                    self._interval,
                )
                started.set()
                await self._transport.publish(
                    item.request.topic, item.request.payload, retain=False
                )
                return True

            publication = asyncio.create_task(publish())
            timer = asyncio.create_task(self._clock.sleep(self._timeout))
            done, _ = await asyncio.wait(
                (publication, timer), return_when=asyncio.FIRST_COMPLETED
            )
            if publication in done:
                result = (
                    SendResult(SendStatus.SENT)
                    if publication.result()
                    else SendResult(SendStatus.STALE, "stale")
                )
            else:
                result = SendResult(SendStatus.FAILED, "delivery_unknown")
        except asyncio.CancelledError:
            result = SendResult(SendStatus.FAILED, "delivery_unknown")
        except Exception:  # noqa: BLE001 - transport failures have unknown delivery
            result = SendResult(SendStatus.FAILED, "delivery_unknown")
        finally:
            self._finalizing.add(item)
            started.set()
            if timer and not timer.done():
                timer.cancel()
            if timer:
                await asyncio.gather(timer, return_exceptions=True)
            if publication and not publication.done():
                publication.cancel()
                await asyncio.sleep(0)
            if publication and not publication.done():
                self._quarantined.update(item.request.resources)
                self._unsettled.add(publication)
                publication.add_done_callback(
                    lambda task, resources=item.request.resources: (
                        self._release_quarantine(task, resources)
                    )
                )
            else:
                if publication:
                    await asyncio.gather(publication, return_exceptions=True)
                self._busy.difference_update(item.request.resources)
            self._running.pop(item, None)
            self._finalizing.discard(item)
            if not item.result.done():
                item.result.set_result(result)
            self._notify()
