"""Event-driven reconciliation of relay-powered circuits (core only)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count

from .command_arbiter import CommandArbiter
from .configuration import kelvin_to_mired
from .desired_store import DesiredStateStore
from .models import (
    CommandKind,
    CommandPriority,
    CommandToken,
    DesiredState,
    EffectiveState,
    EvidenceQuality,
    IntentPatch,
    LightSnapshot,
    ObservationDelta,
    Phase,
    PolicyConfig,
    SendRequest,
    SendResult,
    SendStatus,
    SwitchState,
)
from .observation_store import ObservationStore
from .power_manager import is_ready
from .protocols import Clock, Transport
from .state_machine import Event, StateMachine


@dataclass(frozen=True, slots=True)
class Circuit:
    """Resolved synthetic or runtime address and stable resource identities."""

    light_id: str
    relay_channel_id: str
    relay_topic: str
    led_topic: str
    relay_resource: str
    led_resource: str
    relay_state_key: str


class Reconciler:
    """Reconcile intentions without retaining historical command queues.

    P06 is a core-only component: an eventual HA runtime must supply stable,
    verified addresses and manage control/bridge epochs before enabling it.
    """

    def __init__(
        self,
        *,
        circuits: tuple[Circuit, ...],
        desired: DesiredStateStore,
        observations: ObservationStore,
        transport: Transport,
        clock: Clock,
        policy: PolicyConfig,
        enabled: bool = False,
    ) -> None:
        self._circuits = {item.light_id: item for item in circuits}
        if len(self._circuits) != len(circuits):
            raise ValueError("duplicate light_id")
        if len({c.relay_resource for c in circuits}) != len(circuits):
            raise ValueError("duplicate relay resource")
        self._desired = desired
        self._observations = observations
        self._clock = clock
        self._policy = policy
        self._enabled = enabled
        self._closed = False
        self._control_epoch = 0
        self._topology_version = 0
        self._machines = {key: StateMachine() for key in self._circuits}
        self._reasons: dict[str, str | None] = {key: None for key in self._circuits}
        self._events = {key: asyncio.Event() for key in self._circuits}
        self._subscribers: set[Callable[[frozenset[str]], None]] = set()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._sends: set[asyncio.Task[SendResult]] = set()
        self._relay_pending: dict[str, tuple[float, asyncio.Task[SendResult]]] = {}
        self._off_pending: dict[str, tuple[float, asyncio.Task[SendResult]]] = {}
        self._sequence = count()
        self._arbiter = CommandArbiter(
            transport,
            clock,
            is_current=self._is_current,
            min_send_interval_ms=policy.min_send_interval_ms,
            publication_timeout_ms=policy.publication_timeout_ms,
            slider_coalesce_ms=policy.slider_coalesce_ms,
        )

    def _is_current(self, request: SendRequest) -> bool:
        if (
            self._closed
            or not self._enabled
            or request.topology_version != self._topology_version
        ):
            return False
        for token in request.tokens:
            desired = self._desired.get(token.light_id)
            observed = self._observations.snapshot(token.light_id)
            if (
                desired.generation != token.generation
                or observed.power_epoch != token.power_epoch
                or observed.bridge_epoch != token.bridge_epoch
                or token.control_epoch != self._control_epoch
            ):
                return False
            if request.kind == CommandKind.RELAY_ON and not self._trusted_relay(
                token.light_id, SwitchState.OFF
            ):
                return False
            if request.kind == CommandKind.RELAY_ON and not self._off_interval_elapsed(
                token.light_id
            ):
                return False
            if request.kind == CommandKind.RELAY_OFF and desired.on:
                return False
            if request.kind != CommandKind.RELAY_OFF and not desired.on:
                return False
            if request.kind == CommandKind.LED_SET and not is_ready(
                observed,
                now=self._clock.now(),
                boot_grace_seconds=self._policy.boot_grace_ms / 1000,
            ):
                return False
        return True

    def submit(
        self, light_ids: tuple[str, ...], patch: IntentPatch, *, source: str
    ) -> dict[str, DesiredState]:
        if self._closed or not self._enabled:
            raise RuntimeError("control unavailable")
        if any(item not in self._circuits for item in light_ids):
            raise KeyError("unknown light")
        before = {item: self._desired.get(item) for item in light_ids}
        result = self._desired.apply(light_ids, patch, source=source)
        changed = {item for item in light_ids if result[item] != before[item]}
        if patch.on is False:
            changed.update(
                item
                for item in light_ids
                if not self._trusted_relay(item, SwitchState.OFF)
            )
        for item in changed:
            previous = self._workers.get(item)
            if previous and not previous.done():
                previous.cancel()
            self._workers[item] = asyncio.create_task(
                self._run(item, result[item].generation)
            )
        self._announce(changed)
        return result

    def notify(self, delta: ObservationDelta) -> None:
        for item in delta.light_ids:
            if item in self._events:
                self._events[item].set()
        self._announce(delta.light_ids)

    def subscribe(
        self, callback: Callable[[frozenset[str]], None]
    ) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def _announce(self, light_ids) -> None:
        ids = frozenset(light_ids)
        if ids:
            for callback in tuple(self._subscribers):
                callback(ids)

    def _phase(self, item: str, phase: Phase, reason: str | None = None) -> None:
        events = {
            Phase.POWERING: Event.POWER_REQUESTED,
            Phase.WAITING_READY: Event.POWER_CONFIRMED,
            Phase.APPLYING: Event.READY
            if self._desired.get(item).on
            else Event.OFF_REQUESTED,
            Phase.UNCONFIRMED: Event.SET_SENT,
            Phase.ON: Event.LED_CONFIRMED,
            Phase.OFF: Event.OFF_CONFIRMED,
            Phase.ERROR: Event.FAILED,
        }
        self._machines[item].advance(events[phase])
        self._reasons[item] = reason
        self._announce({item})

    def snapshot(self, light_id: str) -> LightSnapshot:
        observed = self._observations.snapshot(light_id)
        valid_relay = (
            observed.relay_quality == EvidenceQuality.DEVICE_REPORT
            and observed.relay_retained is False
        )
        if valid_relay and observed.relay_state == SwitchState.OFF:
            effective = EffectiveState.OFF_CONFIRMED
        elif (
            valid_relay
            and observed.relay_state == SwitchState.ON
            and observed.led_state == SwitchState.ON
            and observed.led_quality == EvidenceQuality.DEVICE_REPORT
            and observed.led_retained is False
        ):
            effective = EffectiveState.ON_REPORTED
        else:
            effective = EffectiveState.UNKNOWN
        phase = self._machines[light_id].phase
        desired = self._desired.get(light_id)
        confirmed = (
            phase == Phase.OFF
            and not desired.on
            and effective == EffectiveState.OFF_CONFIRMED
        ) or (
            phase == Phase.ON
            and desired.on
            and effective == EffectiveState.ON_REPORTED
            and (
                desired.brightness is None or desired.brightness == observed.brightness
            )
            and (
                desired.color_temp_kelvin is None
                or desired.color_temp_kelvin == observed.color_temp_kelvin
            )
        )
        return LightSnapshot(
            desired,
            phase,
            effective,
            confirmed,
            self._enabled and not self._closed,
            self._reasons[light_id],
        )

    def _token(self, item: str) -> CommandToken:
        obs = self._observations.snapshot(item)
        return CommandToken(
            item,
            self._desired.get(item).generation,
            obs.power_epoch,
            obs.bridge_epoch,
            self._control_epoch,
        )

    def _current(self, item: str, generation: int) -> bool:
        return (
            not self._closed
            and self._enabled
            and self._desired.get(item).generation == generation
        )

    async def _wait(
        self,
        item: str,
        generation: int,
        predicate: Callable[[], bool],
        timeout: float,
        *,
        return_on_event: bool = False,
    ) -> bool:
        deadline = self._clock.now() + timeout
        event = self._events[item]
        while self._current(item, generation):
            event.clear()
            if predicate():
                return True
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                return False
            wake = asyncio.create_task(event.wait())
            timer = asyncio.create_task(self._clock.sleep(remaining))
            try:
                await asyncio.wait((wake, timer), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (wake, timer):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(wake, timer, return_exceptions=True)
            if (
                return_on_event
                and wake.done()
                and not wake.cancelled()
                and wake.result()
            ):
                return predicate()
        return False

    def _send(
        self, item: str, kind: CommandKind, payload: dict[str, object]
    ) -> asyncio.Task[SendResult]:
        circuit = self._circuits[item]
        relay = kind in (CommandKind.RELAY_ON, CommandKind.RELAY_OFF)
        request = SendRequest(
            topic=circuit.relay_topic if relay else circuit.led_topic,
            payload=payload,
            tokens=(self._token(item),),
            resources=(circuit.relay_resource if relay else circuit.led_resource,),
            priority=CommandPriority.OFF
            if kind == CommandKind.RELAY_OFF
            else CommandPriority.NORMAL,
            operation_id=f"{item}-{next(self._sequence)}",
            kind=kind,
            topology_version=self._topology_version,
        )
        task = asyncio.create_task(self._arbiter.send(request))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)
        return task

    def _trusted_relay(
        self, item: str, state: SwitchState, after: float | None = None
    ) -> bool:
        obs = self._observations.snapshot(item)
        return (
            obs.relay_state == state
            and obs.relay_quality == EvidenceQuality.DEVICE_REPORT
            and obs.relay_retained is False
            and obs.relay_received_at is not None
            and (after is None or obs.relay_received_at > after)
        )

    def _off_interval_elapsed(self, item: str) -> bool:
        observation = self._observations.snapshot(item)
        reference = observation.power_off_at
        if reference is None:
            # A first OFF observation does not reveal when the relay switched;
            # waiting from its receipt is conservative, not a physical timestamp.
            reference = observation.relay_received_at
        return reference is not None and (
            self._clock.now() >= reference + self._policy.min_power_off_ms / 1000
        )

    async def _run(self, item: str, generation: int) -> None:
        try:
            if not self._desired.get(item).on:
                await self._run_off(item, generation)
            else:
                await self._run_on(item, generation)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fail closed on an unexpected error
            if self._current(item, generation):
                self._phase(item, Phase.ERROR, "internal_error")

    async def _run_off(self, item: str, generation: int) -> None:
        circuit = self._circuits[item]
        self._phase(item, Phase.APPLYING)
        started = self._clock.now()
        send = self._send(item, CommandKind.RELAY_OFF, {circuit.relay_state_key: "OFF"})
        self._off_pending[item] = (started, send)
        result = await asyncio.shield(send)
        if not self._current(item, generation):
            return
        if result.status != SendStatus.SENT:
            self._phase(item, Phase.ERROR, result.reason or "relay_off_failed")
            return
        confirmed = await self._wait(
            item,
            generation,
            lambda: self._trusted_relay(item, SwitchState.OFF, started),
            self._policy.relay_command_timeout_ms / 1000,
        )
        if not self._current(item, generation):
            return
        if confirmed:
            self._phase(item, Phase.OFF)
        else:
            self._phase(item, Phase.ERROR, "relay_confirmation_timeout")

    async def _run_on(self, item: str, generation: int) -> None:
        circuit = self._circuits[item]
        self._phase(item, Phase.POWERING)
        prior_off = self._off_pending.get(item)
        if prior_off:
            started, send = prior_off
            result = await asyncio.shield(send)
            if not self._current(item, generation):
                return
            if result.status not in (SendStatus.SENT, SendStatus.STALE):
                self._phase(item, Phase.ERROR, result.reason or "relay_off_uncertain")
                return
            if result.status == SendStatus.SENT:
                confirmed = await self._wait(
                    item,
                    generation,
                    lambda: self._trusted_relay(item, SwitchState.OFF, started),
                    self._policy.relay_command_timeout_ms / 1000,
                )
                if not self._current(item, generation):
                    return
                if not confirmed:
                    self._phase(item, Phase.ERROR, "relay_off_confirmation_timeout")
                    return
                off_at = self._observations.snapshot(item).relay_received_at
                delay = (
                    off_at + self._policy.min_power_off_ms / 1000 - self._clock.now()
                )
                if delay > 0:
                    await self._clock.sleep(delay)
                    if not self._current(item, generation):
                        return
            self._off_pending.pop(item, None)
            if result.status == SendStatus.SENT:
                self._relay_pending.pop(item, None)

        pending = self._relay_pending.get(item)
        if pending and not self._trusted_relay(item, SwitchState.ON):
            pending_started, pending_send = pending
            pending_result = await asyncio.shield(pending_send)
            if not self._current(item, generation):
                return
            if pending_result.status == SendStatus.SENT:
                confirmed = await self._wait(
                    item,
                    generation,
                    lambda: self._trusted_relay(item, SwitchState.ON, pending_started),
                    max(
                        0.0,
                        self._policy.relay_command_timeout_ms / 1000
                        - (self._clock.now() - pending_started),
                    ),
                )
                if not self._current(item, generation):
                    return
                if not confirmed:
                    self._phase(item, Phase.ERROR, "relay_confirmation_timeout")
                    return
            elif pending_result.status != SendStatus.STALE:
                self._phase(
                    item, Phase.ERROR, pending_result.reason or "relay_on_failed"
                )
                return
            self._relay_pending.pop(item, None)
        if not self._trusted_relay(item, SwitchState.ON):
            if not self._trusted_relay(item, SwitchState.OFF):
                self._phase(item, Phase.ERROR, "relay_state_unknown")
                return
            observation = self._observations.snapshot(item)
            off_reference = observation.power_off_at
            if off_reference is None:
                off_reference = observation.relay_received_at
            delay = (
                off_reference + self._policy.min_power_off_ms / 1000 - self._clock.now()
            )
            if delay > 0:
                await self._clock.sleep(delay)
                if not self._current(item, generation):
                    return
            if not self._trusted_relay(
                item, SwitchState.OFF
            ) or not self._off_interval_elapsed(item):
                self._phase(item, Phase.ERROR, "relay_off_evidence_changed")
                return
            started = self._clock.now()
            send = self._send(
                item, CommandKind.RELAY_ON, {circuit.relay_state_key: "ON"}
            )
            self._relay_pending[item] = (started, send)
            result = await asyncio.shield(send)
            if not self._current(item, generation):
                return
            if result.status != SendStatus.SENT:
                self._phase(item, Phase.ERROR, result.reason or "relay_on_failed")
                return
            confirmed = await self._wait(
                item,
                generation,
                lambda: self._trusted_relay(item, SwitchState.ON, started),
                self._policy.relay_command_timeout_ms / 1000,
            )
            if not self._current(item, generation):
                return
            if not confirmed:
                self._phase(item, Phase.ERROR, "relay_confirmation_timeout")
                return
        self._phase(item, Phase.WAITING_READY)
        ready_epoch = self._observations.snapshot(item).power_epoch
        bridge_epoch = self._observations.snapshot(item).bridge_epoch
        boot = self._policy.boot_grace_ms / 1000
        deadline = self._clock.now() + self._policy.readiness_timeout_ms / 1000
        while self._current(item, generation):
            obs = self._observations.snapshot(item)
            if (
                obs.power_epoch != ready_epoch
                or obs.bridge_epoch != bridge_epoch
                or not self._trusted_relay(item, SwitchState.ON)
            ):
                self._phase(item, Phase.ERROR, "power_evidence_changed")
                return
            if is_ready(obs, now=self._clock.now(), boot_grace_seconds=boot):
                break
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                self._phase(item, Phase.ERROR, "readiness_timeout")
                return
            # Wake at the grace boundary as well as on reports.
            grace = (
                obs.power_started_at + boot - self._clock.now()
                if obs.power_started_at is not None
                else remaining
            )
            delay = min(remaining, grace) if grace > 0 else remaining
            await self._wait(
                item, generation, lambda: False, delay, return_on_event=True
            )
        if not self._current(item, generation):
            return
        self._phase(item, Phase.APPLYING)
        target = self._desired.get(item)
        obs = self._observations.snapshot(item)
        if (
            obs.led_state == SwitchState.ON
            and (target.brightness is None or target.brightness == obs.brightness)
            and (
                target.color_temp_kelvin is None
                or target.color_temp_kelvin == obs.color_temp_kelvin
            )
        ):
            self._phase(item, Phase.ON)
            return
        payload: dict[str, object] = {"state": "ON", "transition": 0}
        if target.brightness is not None:
            payload["brightness"] = target.brightness
        if target.color_temp_kelvin is not None:
            payload["color_temp"] = kelvin_to_mired(target.color_temp_kelvin)
        result = await asyncio.shield(self._send(item, CommandKind.LED_SET, payload))
        send_completed = self._clock.now()
        if not self._current(item, generation):
            return
        if result.status != SendStatus.SENT:
            self._phase(item, Phase.ERROR, result.reason or "led_set_failed")
            return
        self._phase(item, Phase.UNCONFIRMED)
        confirmed = await self._wait(
            item,
            generation,
            lambda: (
                (obs := self._observations.snapshot(item)).led_state == SwitchState.ON
                and obs.power_epoch == ready_epoch
                and obs.bridge_epoch == bridge_epoch
                and self._trusted_relay(item, SwitchState.ON)
                and obs.led_quality == EvidenceQuality.DEVICE_REPORT
                and obs.led_retained is False
                and obs.led_received_at is not None
                and obs.led_received_at > send_completed
                and (target.brightness is None or target.brightness == obs.brightness)
                and (
                    target.color_temp_kelvin is None
                    or target.color_temp_kelvin == obs.color_temp_kelvin
                )
            ),
            self._policy.verification_timeout_ms / 1000,
        )
        if confirmed and self._current(item, generation):
            self._phase(item, Phase.ON)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        await self._arbiter.close()
        if self._sends:
            await asyncio.gather(*self._sends, return_exceptions=True)
        self._subscribers.clear()
