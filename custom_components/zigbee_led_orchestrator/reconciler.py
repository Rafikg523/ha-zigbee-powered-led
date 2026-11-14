"""Event-driven reconciliation of relay-powered circuits (core only)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Protocol

from .command_arbiter import CommandArbiter
from .configuration import kelvin_to_mired
from .desired_store import DesiredStateStore
from .light_capabilities import LightCapabilities
from .metrics import Metrics
from .models import (
    CommandKind,
    CommandPriority,
    CommandToken,
    DesiredState,
    EffectiveState,
    EvidenceQuality,
    GroupAction,
    GroupDecision,
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

_LOGGER = logging.getLogger(__name__)
_KIND_COUNTERS = {
    CommandKind.RELAY_ON: "relay_on",
    CommandKind.RELAY_OFF: "relay_off",
    CommandKind.LED_SET: "led_set",
    CommandKind.LED_OFF: "led_off",
    CommandKind.GROUP_SET: "group_set",
    CommandKind.PROBE: "probes",
}


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


@dataclass(frozen=True, slots=True)
class GroupCircuit:
    """Verified native group address; members are light ids, not Z2M names."""

    group_id: str
    topic: str
    resource: str
    members: tuple[str, ...]


class GroupGate(Protocol):
    """Barrier owned by a group coordinator for one submitted operation."""

    def begin(self, light_id: str, generation: int) -> None: ...

    async def arrive(
        self, light_id: str, generation: int, needs_set: bool
    ) -> GroupDecision: ...


@dataclass(frozen=True, slots=True)
class InFlightCommand:
    channel_id: str
    target_state: SwitchState
    token: CommandToken
    sent_at: float
    correlation_deadline: float


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
        groups: tuple[GroupCircuit, ...] = (),
        metrics: Metrics | None = None,
        probe_enabled: bool = False,
    ) -> None:
        self._metrics = metrics
        self._probe_enabled = probe_enabled
        self._phase_since: dict[str, float] = {}
        self._circuits = {item.light_id: item for item in circuits}
        if len(self._circuits) != len(circuits):
            raise ValueError("duplicate light_id")
        self._groups = {item.group_id: item for item in groups}
        if (
            len(self._groups) != len(groups)
            or len({g.topic for g in groups}) != len(groups)
            or len({g.resource for g in groups}) != len(groups)
        ):
            raise ValueError("duplicate group")
        for group in groups:
            if (
                not group.members
                or len(set(group.members)) != len(group.members)
                or not set(group.members) <= set(self._circuits)
            ):
                raise ValueError("invalid group members")
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
        self._preparations: set[asyncio.Task[SendResult]] = set()
        self._relay_pending: dict[str, tuple[float, asyncio.Task[SendResult]]] = {}
        self._off_pending: dict[str, tuple[float, asyncio.Task[SendResult]]] = {}
        self._led_off_pending: dict[
            str, tuple[tuple[int, int, int], float, asyncio.Task[SendResult]]
        ] = {}
        self._readiness_deadlines: dict[str, tuple[int, int, float]] = {}
        self._in_flight: dict[str, InFlightCommand] = {}
        self._interrupted_on: dict[str, InFlightCommand] = {}
        self._gates: dict[str, tuple[GroupGate, int]] = {}
        self._last_relay: dict[str, SwitchState] = {}
        self._last_led_publish: dict[str, tuple[str, float]] = {}
        self._capabilities: dict[str, LightCapabilities] = {}
        self._probes: dict[tuple, tuple[int, float, asyncio.Task[SendResult]]] = {}
        self._sequence = count()
        self._arbiter = CommandArbiter(
            transport,
            clock,
            is_current=self._is_current,
            min_send_interval_ms=policy.min_send_interval_ms,
            publication_timeout_ms=policy.publication_timeout_ms,
            slider_coalesce_ms=policy.slider_coalesce_ms,
            on_publish=self._published,
        )

    def _count(self, name: str) -> None:
        if self._metrics is not None:
            self._metrics.count(name)

    def _published(self, request: SendRequest, sent_at: float) -> None:
        self._count("commands_published")
        if request.kind in _KIND_COUNTERS:
            self._count(_KIND_COUNTERS[request.kind])
        _LOGGER.debug(
            "published operation_id=%s kind=%s generations=%s evidence=publication",
            request.operation_id,
            request.kind,
            [token.generation for token in request.tokens],
        )
        if request.kind in (
            CommandKind.LED_SET,
            CommandKind.GROUP_SET,
            CommandKind.LED_OFF,
        ):
            for token in request.tokens:
                self._last_led_publish[token.light_id] = (
                    request.operation_id,
                    float("inf"),
                )
        if request.kind not in (CommandKind.RELAY_ON, CommandKind.RELAY_OFF):
            return
        token = request.tokens[0]
        self._in_flight[token.light_id] = InFlightCommand(
            self._circuits[token.light_id].relay_channel_id,
            SwitchState.ON if request.kind == CommandKind.RELAY_ON else SwitchState.OFF,
            token,
            sent_at,
            sent_at + self._policy.relay_command_timeout_ms / 1000,
        )

    def _is_current(self, request: SendRequest) -> bool:
        if (
            self._closed
            or not self._enabled
            or self._observations.bridge_online is False
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
            if request.kind == CommandKind.PROBE:
                circuit = self._circuits[token.light_id]
                relay_get = circuit.relay_topic.removesuffix("/set") + "/get"
                led_get = circuit.led_topic.removesuffix("/set") + "/get"
                if request.topic not in (relay_get, led_get):
                    return False
                if request.topic == led_get and not self._trusted_relay(
                    token.light_id, SwitchState.ON
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
            if (
                request.kind in (CommandKind.RELAY_OFF, CommandKind.LED_OFF)
                and desired.on
            ):
                return False
            if request.kind == CommandKind.LED_OFF and not self._trusted_relay(
                token.light_id, SwitchState.ON
            ):
                return False
            if (
                request.kind
                not in (CommandKind.RELAY_OFF, CommandKind.LED_OFF, CommandKind.PROBE)
                and not desired.on
            ):
                return False
            if request.kind in (
                CommandKind.LED_SET,
                CommandKind.GROUP_SET,
            ) and not is_ready(
                observed,
                now=self._clock.now(),
                boot_grace_seconds=self._policy.boot_grace_ms / 1000,
            ):
                return False
        if request.kind == CommandKind.GROUP_SET:
            group = next(
                (g for g in self._groups.values() if g.topic == request.topic), None
            )
            if group is None or {t.light_id for t in request.tokens} != set(
                group.members
            ):
                return False
        return True

    @property
    def groups(self) -> dict[str, GroupCircuit]:
        return dict(self._groups)

    def set_capabilities(self, light_id: str, capabilities: LightCapabilities) -> None:
        """Set verified device ranges before starting a new operation."""
        if light_id not in self._circuits:
            raise KeyError(light_id)
        if self._desired.get(light_id).on:
            raise RuntimeError("cannot change capabilities while light is requested on")
        self._capabilities[light_id] = capabilities

    def _led_matches(self, item, desired, observed) -> bool:
        published = self._last_led_publish.get(item)
        return (
            observed.led_state == SwitchState.ON
            and observed.led_quality == EvidenceQuality.DEVICE_REPORT
            and observed.led_retained is False
            and observed.led_received_at is not None
            and (published is None or observed.led_received_at > published[1])
            and self._matches_parameters(item, desired, observed)
        )

    async def _send_request(self, request: SendRequest) -> SendResult:
        result = await self._arbiter.send(request)
        if result.status == SendStatus.SENT:
            for token in request.tokens:
                published = self._last_led_publish.get(token.light_id)
                if published is not None and published[0] == request.operation_id:
                    self._last_led_publish[token.light_id] = (
                        request.operation_id,
                        self._clock.now(),
                    )
        return result

    def _matches_parameters(self, item, desired, observed) -> bool:
        capabilities = self._capabilities.get(item)
        brightness = desired.brightness
        kelvin = desired.color_temp_kelvin
        if capabilities is not None:
            if brightness is not None:
                brightness = capabilities.device_brightness(brightness)
            if kelvin is not None:
                kelvin = round(1_000_000 / capabilities.device_mired(kelvin))
        return (brightness is None or brightness == observed.brightness) and (
            kelvin is None or kelvin == observed.color_temp_kelvin
        )

    def set_enabled(self, enabled: bool) -> None:
        """Revoke pending authority when control changes; never replay old ON."""
        if self._closed:
            raise RuntimeError("reconciler closed")
        if type(enabled) is not bool:
            raise TypeError("enabled must be boolean")
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._control_epoch += 1
        if not enabled:
            for item in self._circuits:
                worker = self._workers.get(item)
                if worker and not worker.done():
                    worker.cancel()
                self._desired.apply(
                    (item,), IntentPatch(on=False), source="control_disabled"
                )
                self._readiness_deadlines.pop(item, None)
                # Commands already handed to transport cannot be cancelled.
                # Keep their pending state so a later explicit ON waits for OFF.
                self._machines[item].advance(Event.OFF_REQUESTED)
                self._phase(item, Phase.ERROR, "control_disabled")
        self._announce(self._circuits)

    def submit(
        self,
        light_ids: tuple[str, ...],
        patch: IntentPatch,
        *,
        source: str,
        gate: GroupGate | None = None,
    ) -> dict[str, DesiredState]:
        if (
            self._closed
            or not self._enabled
            or self._observations.bridge_online is False
        ):
            raise RuntimeError("control unavailable")
        if any(item not in self._circuits for item in light_ids):
            raise KeyError("unknown light")
        for item in light_ids:
            capability = self._capabilities.get(item)
            if capability is not None:
                if patch.brightness is not None:
                    capability.device_brightness(patch.brightness)
                if patch.color_temp_kelvin is not None:
                    capability.device_mired(patch.color_temp_kelvin)
        before = {item: self._desired.get(item) for item in light_ids}
        # A completed read failure must not require an artificial OFF/ON dance.
        # Renew authority only for a new explicit request, never from a report.
        renew = (
            self._probe_enabled
            and patch.on is True
            and any(
                self._machines[item].phase == Phase.ERROR
                and self._reasons[item]
                in ("relay_read_timeout", "relay_read_failed", "readiness_timeout")
                and (worker := self._workers.get(item)) is not None
                and worker.done()
                for item in light_ids
            )
        )
        if renew:
            self._probes = {
                k: v for k, v in self._probes.items() if k[0] not in light_ids
            }
        result = self._desired.apply(light_ids, patch, source=source, force=renew)
        changed = {item for item in light_ids if result[item] != before[item]}
        if patch.on is False:
            changed.update(
                item
                for item in light_ids
                if not self._trusted_relay(item, SwitchState.OFF)
            )
        for item in changed:
            if result[item].on:
                pending_off = self._led_off_pending.pop(item, None)
                if pending_off is not None:
                    pending_off[2].cancel()
            previous = self._workers.get(item)
            if (
                patch.on is None
                and not result[item].on
                and not before[item].on
                and (previous is None or previous.done())
            ):
                continue
            if previous and not previous.done():
                previous.cancel()
                self._count("cancellations")
            self._workers[item] = asyncio.create_task(
                self._run(item, result[item].generation)
            )
            if gate is not None and result[item].on:
                self._gates[item] = (gate, result[item].generation)
                gate.begin(item, result[item].generation)
        self._announce(changed)
        return result

    def release_gate(self, gate: GroupGate) -> None:
        for item, (owner, _generation) in tuple(self._gates.items()):
            if owner is gate:
                del self._gates[item]

    def abort_member(self, light_id: str, generation: int, reason: str) -> None:
        """Fail one still-requested member without touching its relay or intent."""
        if (
            self._closed
            or light_id not in self._circuits
            or self._desired.get(light_id).generation != generation
        ):
            return
        worker = self._workers.get(light_id)
        if worker and not worker.done():
            worker.cancel()
        self._readiness_deadlines.pop(light_id, None)
        self._phase(light_id, Phase.ERROR, reason)

    def topology_changed(self, version: int) -> None:
        """Revoke all authority; a new topology must be validated and re-enabled."""
        self._topology_version = version
        if self._enabled:
            self.set_enabled(False)
        else:
            self._control_epoch += 1

    def notify(self, delta: ObservationDelta) -> None:
        if delta.bridge_changed and self._observations.bridge_online is False:
            self._control_epoch += 1
            for item in self._circuits:
                worker = self._workers.get(item)
                if worker and not worker.done():
                    worker.cancel()
                self._desired.apply(
                    (item,), IntentPatch(on=False), source="bridge_offline"
                )
                self._readiness_deadlines.pop(item, None)
                flight = self._in_flight.pop(item, None)
                if flight and flight.target_state == SwitchState.ON:
                    self._interrupted_on[item] = flight
                self._relay_pending.pop(item, None)
                self._last_relay.pop(item, None)
                self._machines[item].advance(Event.OFF_REQUESTED)
                self._phase(item, Phase.ERROR, "bridge_offline")
        for item in delta.light_ids:
            if item in self._events:
                if not self._enabled:
                    self._last_relay.pop(item, None)
                    self._events[item].set()
                    continue
                obs = self._observations.snapshot(item)
                flight = self._in_flight.get(item)
                matched = (
                    flight is not None
                    and self._trusted_relay(item, flight.target_state)
                    and flight.token.bridge_epoch == obs.bridge_epoch
                    and obs.relay_received_at is not None
                    and flight.sent_at
                    < obs.relay_received_at
                    <= flight.correlation_deadline
                )
                own_off = matched and flight.target_state == SwitchState.OFF
                if matched:
                    self._in_flight.pop(item, None)
                interrupted = self._interrupted_on.get(item)
                late_on = (
                    interrupted is not None
                    and self._trusted_relay(item, SwitchState.ON)
                    and obs.relay_received_at is not None
                    and interrupted.sent_at
                    < obs.relay_received_at
                    <= interrupted.correlation_deadline
                    and not self._desired.get(item).on
                )
                if late_on:
                    self._interrupted_on.pop(item, None)
                    previous = self._workers.get(item)
                    if previous and not previous.done():
                        previous.cancel()
                    self._workers[item] = asyncio.create_task(
                        self._run(item, self._desired.get(item).generation)
                    )
                if (
                    self._desired.get(item).on
                    and self._trusted_relay(item, SwitchState.OFF)
                    and obs.power_off_at is not None
                    and obs.power_off_at == obs.relay_received_at
                    and not own_off
                ):
                    self._count("conflicts")
                    self._desired.apply(
                        (item,), IntentPatch(on=False), source="external_relay"
                    )
                    worker = self._workers.get(item)
                    if worker and not worker.done():
                        worker.cancel()
                    self._readiness_deadlines.pop(item, None)
                    self._machines[item].advance(Event.OFF_REQUESTED)
                    self._phase(item, Phase.OFF)
                elif (
                    self._last_relay.get(item) == SwitchState.OFF
                    and self._trusted_relay(item, SwitchState.ON)
                    and not matched
                    and not late_on
                    and not self._desired.get(item).on
                ):
                    self._count("conflicts")
                    state = self._desired.apply(
                        (item,), IntentPatch(on=True), source="external_relay"
                    )[item]
                    worker = self._workers.get(item)
                    if worker and not worker.done():
                        worker.cancel()
                    self._workers[item] = asyncio.create_task(
                        self._observe_external_on(item, state.generation)
                    )
                if self._trusted_relay(item, SwitchState.ON):
                    self._last_relay[item] = SwitchState.ON
                elif self._trusted_relay(item, SwitchState.OFF):
                    self._last_relay[item] = SwitchState.OFF
                else:
                    self._last_relay.pop(item, None)
                if (
                    self._probe_enabled
                    and (worker := self._workers.get(item)) is not None
                    and worker.done()
                    and self._machines[item].phase == Phase.UNCONFIRMED
                    and self._desired.get(item).on
                    and self._trusted_relay(item, SwitchState.ON)
                    and self._led_matches(item, self._desired.get(item), obs)
                ):
                    # Fresh matching reports may finish verification after the
                    # bounded worker has stopped; they never trigger a command.
                    self._phase(item, Phase.ON)
                self._events[item].set()
        self._announce(delta.light_ids)

    async def _observe_external_on(self, item: str, generation: int) -> None:
        self._phase(item, Phase.POWERING)
        self._phase(item, Phase.WAITING_READY)
        epoch = self._observations.snapshot(item).power_epoch
        bridge = self._observations.snapshot(item).bridge_epoch
        deadline = self._clock.now() + self._policy.readiness_timeout_ms / 1000
        ready = False
        while self._current(item, generation):
            obs = self._observations.snapshot(item)
            if obs.power_epoch != epoch or obs.bridge_epoch != bridge:
                break
            if is_ready(
                obs,
                now=self._clock.now(),
                boot_grace_seconds=self._policy.boot_grace_ms / 1000,
            ):
                ready = True
                break
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                break
            if (
                self._probe_enabled
                and obs.power_started_at is not None
                and self._clock.now()
                >= obs.power_started_at + self._policy.boot_grace_ms / 1000
            ):
                result, _started = await self._probe(item, "ready")
                if not self._current(item, generation):
                    return
                if result.status != SendStatus.SENT:
                    break
                if is_ready(
                    self._observations.snapshot(item),
                    now=self._clock.now(),
                    boot_grace_seconds=self._policy.boot_grace_ms / 1000,
                ):
                    continue
            grace = (
                (obs.power_started_at or self._clock.now())
                + self._policy.boot_grace_ms / 1000
                - self._clock.now()
            )
            await self._wait(
                item,
                generation,
                lambda: False,
                min(remaining, grace) if grace > 0 else remaining,
                return_on_event=True,
            )
        obs = self._observations.snapshot(item)
        if not self._current(item, generation):
            return
        if not ready or obs.power_epoch != epoch or obs.bridge_epoch != bridge:
            self._phase(item, Phase.ERROR, "readiness_timeout")
            return
        if self._policy.restore_parameters:
            await self._run_on(item, generation)
            return
        self._phase(item, Phase.APPLYING)
        if obs.led_state == SwitchState.ON:
            self._phase(item, Phase.ON)
        else:
            self._phase(item, Phase.UNCONFIRMED)

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
        now = self._clock.now()
        _LOGGER.debug(
            "phase light=%s generation=%s phase=%s reason=%s duration_ms=%s",
            item,
            self._desired.get(item).generation,
            phase,
            reason,
            round((now - self._phase_since.get(item, now)) * 1000),
        )
        self._phase_since[item] = now
        if phase == Phase.ERROR:
            if reason in ("control_disabled", "bridge_offline"):
                self._count("cancellations")
            else:
                self._count("errors")
                if reason is not None and "timeout" in reason:
                    self._count("timeouts")
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
            and self._led_matches(light_id, desired, observed)
        )
        return LightSnapshot(
            desired,
            phase,
            effective,
            confirmed,
            self._enabled
            and not self._closed
            and self._observations.bridge_online is not False,
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
            and self._observations.bridge_online is not False
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
        self,
        item: str,
        kind: CommandKind,
        payload: dict[str, object],
        *,
        probe_relay: bool = False,
    ) -> asyncio.Task[SendResult]:
        circuit = self._circuits[item]
        relay = kind in (CommandKind.RELAY_ON, CommandKind.RELAY_OFF) or probe_relay
        topic = circuit.relay_topic if relay else circuit.led_topic
        if kind == CommandKind.PROBE:
            topic = topic.removesuffix("/set") + "/get"
        request = SendRequest(
            topic=topic,
            payload=payload,
            tokens=(self._token(item),),
            resources=(circuit.relay_resource if relay else circuit.led_resource,),
            priority=CommandPriority.OFF
            if kind in (CommandKind.RELAY_OFF, CommandKind.LED_OFF)
            or (kind == CommandKind.PROBE and not self._desired.get(item).on)
            else CommandPriority.NORMAL,
            operation_id=f"{item}-{next(self._sequence)}",
            kind=kind,
            topology_version=self._topology_version,
        )
        task = asyncio.create_task(self._send_request(request))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)
        return task

    async def _probe(self, item: str, stage: str) -> tuple[SendResult, float]:
        """One bounded read per stage; slider changes share an outstanding read."""
        obs = self._observations.snapshot(item)
        generation = self._desired.get(item).generation
        marker = self._last_led_publish.get(item) if stage == "verify" else None
        key = (
            item,
            stage,
            obs.power_epoch,
            obs.bridge_epoch,
            self._control_epoch,
            marker,
        )
        previous = self._probes.get(key)
        timeout = (
            self._policy.relay_command_timeout_ms
            if stage == "relay"
            else self._policy.readiness_timeout_ms
        ) / 1000
        if previous is not None:
            old_generation, started, task = previous
            result = await asyncio.shield(task)
            if result.status != SendStatus.STALE and (
                old_generation == generation or self._clock.now() < started + timeout
            ):
                return result, started
        # Keep only the current attempt for each light/stage.
        self._probes = {k: v for k, v in self._probes.items() if k[:2] != key[:2]}
        payload = {"state": ""}
        if stage == "relay":
            payload = {self._circuits[item].relay_state_key: ""}
        elif stage == "verify":
            target = self._desired.get(item)
            if target.brightness is not None:
                payload["brightness"] = ""
            if target.color_temp_kelvin is not None:
                payload["color_temp"] = ""
        started = self._clock.now()
        task = self._send(
            item, CommandKind.PROBE, payload, probe_relay=stage == "relay"
        )
        self._probes[key] = (generation, started, task)
        return await asyncio.shield(task), started

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

    async def _prepare_led_off(
        self, item: str, epoch: tuple[int, int, int], deadline: float
    ) -> SendResult:
        def authorized() -> bool:
            obs = self._observations.snapshot(item)
            return (
                not self._closed
                and self._enabled
                and not self._desired.get(item).on
                and (obs.power_epoch, obs.bridge_epoch, self._control_epoch) == epoch
                and self._trusted_relay(item, SwitchState.ON)
                and self._clock.now() < deadline
            )

        result = SendResult(SendStatus.STALE, "stale")
        while authorized():
            result = await asyncio.shield(
                self._send(item, CommandKind.LED_OFF, {"state": "OFF", "transition": 0})
            )
            # STALE was never published. A parameter-only update may renew
            # its token inside this same OFF operation and original deadline.
            if result.status != SendStatus.STALE:
                break
        if result.status != SendStatus.SENT:
            return result
        while authorized():
            result, _started = await self._probe(item, "off")
            if result.status != SendStatus.STALE:
                break
        return result

    async def _confirm_off_before_power_cut(
        self, item: str, generation: int
    ) -> str | None:
        """Try a confirmed LED OFF; the relay cutoff remains bounded and mandatory."""
        obs = self._observations.snapshot(item)
        epoch = (obs.power_epoch, obs.bridge_epoch, self._control_epoch)
        pending = self._led_off_pending.get(item)
        if pending is None or pending[0] != epoch:
            started = self._clock.now()
            if pending is not None:
                pending[2].cancel()
            task = asyncio.create_task(
                self._prepare_led_off(
                    item, epoch, started + self._policy.verification_timeout_ms / 1000
                )
            )
            self._preparations.add(task)

            def prepared(done):
                self._preparations.discard(done)
                if not done.cancelled():
                    done.exception()
                self._events[item].set()

            task.add_done_callback(prepared)
            self._led_off_pending[item] = (epoch, started, task)
        else:
            _epoch, started, task = pending
        deadline = started + self._policy.verification_timeout_ms / 1000
        await self._wait(
            item,
            generation,
            lambda: task.done() or not self._trusted_relay(item, SwitchState.ON),
            max(0.0, deadline - self._clock.now()),
        )
        if not self._current(item, generation):
            return None
        if self._trusted_relay(item, SwitchState.OFF):
            return None
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._led_off_pending.pop(item, None)
            self._count("timeouts")
            return "led_off_confirmation_timeout"
        if (
            task.cancelled()
            or task.exception() is not None
            or task.result().status != SendStatus.SENT
        ):
            return "led_off_failed"
        confirmed = await self._wait(
            item,
            generation,
            lambda: (
                self._trusted_relay(item, SwitchState.OFF)
                or (
                    (current := self._observations.snapshot(item)).power_epoch
                    == epoch[0]
                    and current.bridge_epoch == epoch[1]
                    and current.led_state == SwitchState.OFF
                    and current.led_quality == EvidenceQuality.DEVICE_REPORT
                    and current.led_retained is False
                    and current.led_received_at is not None
                    and current.led_received_at
                    > self._last_led_publish.get(item, ("unknown", float("inf")))[1]
                )
            ),
            max(0.0, deadline - self._clock.now()),
        )
        if not self._current(item, generation):
            return None
        if not confirmed:
            self._count("timeouts")
            return "led_off_confirmation_timeout"
        return None

    async def _run_off(self, item: str, generation: int) -> None:
        circuit = self._circuits[item]
        self._readiness_deadlines.pop(item, None)
        self._phase(item, Phase.APPLYING)
        warning = None
        if (
            self._probe_enabled
            and self._trusted_relay(item, SwitchState.ON)
            and item not in self._off_pending
        ):
            warning = await self._confirm_off_before_power_cut(item, generation)
            if not self._current(item, generation):
                return
            if self._trusted_relay(item, SwitchState.OFF):
                self._led_off_pending.pop(item, None)
                self._relay_pending.pop(item, None)
                self._phase(item, Phase.OFF, warning)
                return
        pending = self._off_pending.get(item)
        if pending and self._clock.now() < (
            pending[0] + self._policy.relay_command_timeout_ms / 1000
        ):
            started, send = pending
            result = await asyncio.shield(send)
            if not self._current(item, generation):
                return
        else:
            result = None
        if result is None or result.status == SendStatus.STALE:
            started = self._clock.now()
            send = self._send(
                item, CommandKind.RELAY_OFF, {circuit.relay_state_key: "OFF"}
            )
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
            max(
                0.0,
                self._policy.relay_command_timeout_ms / 1000
                - (self._clock.now() - started),
            ),
        )
        if not self._current(item, generation):
            return
        if confirmed:
            self._off_pending.pop(item, None)
            # A confirmed explicit OFF also resolves an earlier ON timeout.
            # Only a new user intent may start the next ON command.
            self._relay_pending.pop(item, None)
            self._led_off_pending.pop(item, None)
            self._phase(item, Phase.OFF, warning)
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

        if self._probe_enabled and not (
            self._trusted_relay(item, SwitchState.ON)
            or self._trusted_relay(item, SwitchState.OFF)
        ):
            result, started = await self._probe(item, "relay")
            if not self._current(item, generation):
                return
            if result.status != SendStatus.SENT:
                self._phase(item, Phase.ERROR, "relay_read_failed")
                return
            known = await self._wait(
                item,
                generation,
                lambda: (
                    self._trusted_relay(item, SwitchState.ON, started)
                    or self._trusted_relay(item, SwitchState.OFF, started)
                ),
                max(
                    0.0,
                    started
                    + self._policy.relay_command_timeout_ms / 1000
                    - self._clock.now(),
                ),
            )
            if not self._current(item, generation):
                return
            if not known:
                self._phase(item, Phase.ERROR, "relay_read_timeout")
                return

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
        # Confirmed power ends this ON command's lifetime. Keeping its task
        # would make a later power cycle wait on the old, expired deadline.
        self._relay_pending.pop(item, None)
        self._phase(item, Phase.WAITING_READY)
        ready_epoch = self._observations.snapshot(item).power_epoch
        bridge_epoch = self._observations.snapshot(item).bridge_epoch
        boot = self._policy.boot_grace_ms / 1000
        previous_deadline = self._readiness_deadlines.get(item)
        if previous_deadline and previous_deadline[:2] == (ready_epoch, bridge_epoch):
            deadline = previous_deadline[2]
        else:
            deadline = self._clock.now() + self._policy.readiness_timeout_ms / 1000
            self._readiness_deadlines[item] = (ready_epoch, bridge_epoch, deadline)
        while self._current(item, generation):
            obs = self._observations.snapshot(item)
            if (
                obs.power_epoch != ready_epoch
                or obs.bridge_epoch != bridge_epoch
                or not self._trusted_relay(item, SwitchState.ON)
            ):
                self._readiness_deadlines.pop(item, None)
                self._phase(item, Phase.ERROR, "power_evidence_changed")
                return
            if is_ready(obs, now=self._clock.now(), boot_grace_seconds=boot):
                break
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                self._readiness_deadlines.pop(item, None)
                self._phase(item, Phase.ERROR, "readiness_timeout")
                return
            if (
                self._probe_enabled
                and obs.power_started_at is not None
                and self._clock.now() >= obs.power_started_at + boot
            ):
                result, _started = await self._probe(item, "ready")
                if not self._current(item, generation):
                    return
                if result.status != SendStatus.SENT:
                    self._phase(
                        item, Phase.ERROR, result.reason or "readiness_read_failed"
                    )
                    return
                # Recheck reports that may have arrived during publication.
                if is_ready(
                    self._observations.snapshot(item),
                    now=self._clock.now(),
                    boot_grace_seconds=boot,
                ):
                    continue
            # A queued read must not extend the original readiness deadline.
            remaining = max(0.0, deadline - self._clock.now())
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
        if self._metrics is not None and obs.power_started_at is not None:
            self._metrics.record_boot(
                obs.led_quality.value, self._clock.now() - obs.power_started_at
            )
        self._readiness_deadlines.pop(item, None)
        self._phase(item, Phase.APPLYING)
        target = self._desired.get(item)
        obs = self._observations.snapshot(item)
        registration = self._gates.get(item)
        if registration is not None and registration[1] == generation:
            decision = await registration[0].arrive(
                item,
                generation,
                not self._led_matches(item, target, obs),
            )
            if not self._current(item, generation):
                return
            if decision.action == GroupAction.ABORT:
                self._phase(item, Phase.ERROR, decision.reason or "group_set_failed")
                return
            if decision.action == GroupAction.GROUP_SENT:
                self._phase(item, Phase.UNCONFIRMED)
                await self._confirm_led(
                    item,
                    generation,
                    target,
                    decision.sent_at,
                    ready_epoch,
                    bridge_epoch,
                )
                return
            target = self._desired.get(item)
            obs = self._observations.snapshot(item)
        if self._led_matches(item, target, obs):
            self._phase(item, Phase.ON)
            return
        payload = self.led_payload(item)
        result = await asyncio.shield(self._send(item, CommandKind.LED_SET, payload))
        send_completed = self._clock.now()
        if not self._current(item, generation):
            return
        if result.status != SendStatus.SENT:
            self._phase(item, Phase.ERROR, result.reason or "led_set_failed")
            return
        self._phase(item, Phase.UNCONFIRMED)
        await self._confirm_led(
            item, generation, target, send_completed, ready_epoch, bridge_epoch
        )

    async def _confirm_led(
        self,
        item: str,
        generation: int,
        target: DesiredState,
        send_completed: float,
        ready_epoch: int,
        bridge_epoch: int,
    ) -> None:
        if self._probe_enabled:
            result, _started = await self._probe(item, "verify")
            if not self._current(item, generation):
                return
            if result.status != SendStatus.SENT:
                self._reasons[item] = result.reason or "verification_read_failed"
                self._announce({item})
                return
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
                and self._matches_parameters(item, target, obs)
            ),
            self._policy.verification_timeout_ms / 1000,
        )
        if self._current(item, generation):
            if confirmed:
                self._phase(item, Phase.ON)
            elif self._probe_enabled and self._machines[item].phase != Phase.ON:
                self._reasons[item] = "verification_timeout"
                self._count("timeouts")
                self._announce({item})

    def led_payload(self, light_id: str) -> dict[str, object]:
        """Device-range command payload for the member's current desired state."""
        target = self._desired.get(light_id)
        capability = self._capabilities.get(light_id)
        payload: dict[str, object] = {"state": "ON", "transition": 0}
        if target.brightness is not None:
            payload["brightness"] = (
                capability.device_brightness(target.brightness)
                if capability
                else target.brightness
            )
        if target.color_temp_kelvin is not None:
            payload["color_temp"] = (
                capability.device_mired(target.color_temp_kelvin)
                if capability
                else kelvin_to_mired(target.color_temp_kelvin)
            )
        return payload

    def send_group(
        self, group_id: str, payload: dict[str, object], operation_id: str
    ) -> asyncio.Task[SendResult]:
        """Publish one native group command covering every configured member."""
        group = self._groups[group_id]
        request = SendRequest(
            topic=group.topic,
            payload=payload,
            tokens=tuple(self._token(item) for item in group.members),
            resources=(
                group.resource,
                *(self._circuits[item].led_resource for item in group.members),
            ),
            priority=CommandPriority.NORMAL,
            operation_id=f"{operation_id}-{next(self._sequence)}",
            kind=CommandKind.GROUP_SET,
            topology_version=self._topology_version,
        )
        task = asyncio.create_task(self._send_request(request))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)
        return task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        preparations = tuple(self._preparations)
        for task in (*self._workers.values(), *preparations):
            task.cancel()
        await asyncio.gather(
            *self._workers.values(), *preparations, return_exceptions=True
        )
        await self._arbiter.close()
        if self._sends:
            await asyncio.gather(*self._sends, return_exceptions=True)
        self._subscribers.clear()
