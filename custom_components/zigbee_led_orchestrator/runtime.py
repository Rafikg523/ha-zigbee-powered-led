"""Coordinate the HA MQTT adapter and core without implicit activation."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

from .const import RUNTIME_VERSION
from .desired_store import DesiredStateStore
from .group_coordinator import GroupCoordinator
from .light_capabilities import LightCapabilities
from .metrics import Metrics
from .models import (
    BridgeObservation,
    ControlMode,
    EffectiveState,
    GroupSnapshot,
    IntentPatch,
    LightSnapshot,
    Phase,
)
from .observation_store import ObservationStore
from .preferences import Preference
from .reconciler import Circuit, GroupCircuit, Reconciler
from .topology import validate_topology

CONTROL_NOTICE = (
    "Desired state, publication and device reports are separate facts. A published "
    "command or an optimistic or cached report does not prove physical light output."
)


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


def _intersect(ranges):
    if any(item is None for item in ranges):
        return None
    low = max(item[0] for item in ranges)
    high = min(item[1] for item in ranges)
    return (low, high) if low <= high else None


class Runtime:
    """Own a single reconciler and revoke its authority on topology changes."""

    def __init__(self, config, adapter) -> None:
        self.config = config
        self.adapter = adapter
        self.mode = ControlMode.OBSERVE
        self._clock = MonotonicClock()
        self._desired = DesiredStateStore(
            (light.id for light in config.lights), clock=self._clock
        )
        self._observations = ObservationStore(
            {light.id: light.power_channel for light in config.lights}
        )
        self.metrics = Metrics()
        self.preferences_error: str | None = None
        self._preferences_listener: Callable[[], None] | None = None
        self._paused_from = ControlMode.OBSERVE
        self._engine: Reconciler | None = None
        self._groups: GroupCoordinator | None = None
        self._stale = False
        self._closed = False
        self._subscribers: set[Callable[[frozenset[str]], None]] = set()
        self._remove_topology = None

    async def start(self) -> None:
        self._remove_topology = self.adapter.subscribe_topology(self._topology_changed)
        try:
            await self.adapter.start(self._observation)
        except BaseException:
            await self.close()
            raise

    def _topology_changed(self, version: int) -> None:
        if self._engine is not None:
            self._stale = True
            self._engine.topology_changed(version)
            self.adapter.set_active(False)
            self.mode = ControlMode.OBSERVE
        self._announce(frozenset(light.id for light in self.config.lights))

    def _observation(self, observation) -> None:
        delta = self._observations.ingest(observation)
        if isinstance(observation, BridgeObservation) and not observation.online:
            self.mode = ControlMode.OBSERVE
            self.adapter.set_active(False)
            if self._engine is not None:
                self._engine.set_enabled(False)
        if self._engine is not None:
            self._engine.notify(delta)
        else:
            self._announce(delta.light_ids)

    def _announce(self, ids: frozenset[str]) -> None:
        for callback in tuple(self._subscribers):
            callback(ids)

    def subscribe(
        self, callback: Callable[[frozenset[str]], None]
    ) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def light_capabilities(self, light_id: str) -> LightCapabilities:
        light = next(item for item in self.config.lights if item.id == light_id)
        try:
            device = self.adapter.topology.resolve_device(light.device_ieee)
            return LightCapabilities.from_exposes(
                device.capabilities, endpoint=device.endpoint_name(light.endpoint)
            )
        except KeyError, ValueError, TypeError:
            return LightCapabilities(None, None)

    def _make_engine(self) -> Reconciler:
        validate_topology(self.config, self.adapter.topology)
        channels = {channel.id: channel for channel in self.config.relay_channels}
        circuits = []
        for light in self.config.lights:
            channel = channels[light.power_channel]
            relay = self.adapter.topology.resolve_device(channel.device_ieee)
            led = self.adapter.topology.resolve_device(light.device_ieee)
            circuits.append(
                Circuit(
                    light.id,
                    channel.id,
                    relay.topic + "/set",
                    led.topic + "/set",
                    f"{self.config.base_topic}:{channel.device_ieee}:{channel.endpoint}:{channel.state_key}",
                    f"{self.config.base_topic}:{light.device_ieee}:{light.endpoint}",
                    channel.state_key,
                )
            )
        lights = {light.id: light for light in self.config.lights}
        groups = []
        for group in self.config.groups:
            native = self.adapter.topology.resolve_group(group.z2m_id)
            groups.append(
                GroupCircuit(
                    group.id,
                    native.topic + "/set",
                    f"{self.config.base_topic}:group:{group.z2m_id}",
                    group.members,
                )
            )
            if not set(group.members) <= set(lights):
                raise ValueError("group member is not a configured light")
        engine = Reconciler(
            circuits=tuple(circuits),
            desired=self._desired,
            observations=self._observations,
            transport=self.adapter,
            clock=self._clock,
            policy=self.config.policies,
            groups=tuple(groups),
            metrics=self.metrics,
            probe_enabled=self.config.report_extension,
        )
        self._groups = GroupCoordinator(
            engine,
            self._clock,
            barrier_ms=self.config.policies.group_barrier_timeout_ms,
        )
        for light in self.config.lights:
            device = self.adapter.topology.resolve_device(light.device_ieee)
            capabilities = LightCapabilities.from_exposes(
                device.capabilities, endpoint=device.endpoint_name(light.endpoint)
            )
            engine.set_capabilities(light.id, capabilities)
        engine.subscribe(self._announce)
        return engine

    def set_mode(self, mode: ControlMode) -> None:
        if self._closed or not isinstance(mode, ControlMode):
            raise RuntimeError("invalid or closed runtime")
        if mode == ControlMode.ACTIVE:
            if self._stale:
                raise RuntimeError("topology changed; reload required")
            if self._engine is None:
                self._engine = self._make_engine()
            self.adapter.set_active(True)
            self._engine.set_enabled(True)
        else:
            if self._engine is not None:
                self._engine.set_enabled(False)
            self.adapter.set_active(False)
        self.mode = mode
        self._announce(frozenset(light.id for light in self.config.lights))

    def submit(self, light_ids: tuple[str, ...], patch: IntentPatch, *, source: str):
        if self.mode != ControlMode.ACTIVE or self._engine is None or self._closed:
            raise RuntimeError("control unavailable")
        result = self._engine.submit(light_ids, patch, source=source)
        self._parameters_submitted(patch)
        return result

    def _parameters_submitted(self, patch: IntentPatch) -> None:
        if (
            patch.brightness is not None or patch.color_temp_kelvin is not None
        ) and self._preferences_listener is not None:
            self._preferences_listener()

    def set_preferences_listener(self, listener: Callable[[], None] | None) -> None:
        self._preferences_listener = listener

    def preferences(self) -> dict[str, Preference]:
        """Return current parameters only; power and commands are never included."""
        return {
            light.id: Preference(
                self._desired.get(light.id).brightness,
                self._desired.get(light.id).color_temp_kelvin,
            )
            for light in self.config.lights
        }

    def restore_preferences(self, preferences: dict[str, Preference]) -> None:
        """Load saved parameters before control exists; never changes power."""
        if self._closed or self._engine is not None or self.mode != ControlMode.OBSERVE:
            raise RuntimeError("preferences can only be restored before control")
        for light_id, item in preferences.items():
            if item.brightness is None and item.color_temp_kelvin is None:
                continue
            self._desired.apply(
                (light_id,),
                IntentPatch(
                    brightness=item.brightness, color_temp_kelvin=item.color_temp_kelvin
                ),
                source="restored_preferences",
            )

    def submit_group(self, group_id: str, patch: IntentPatch, *, source: str):
        if (
            self.mode != ControlMode.ACTIVE
            or self._engine is None
            or self._groups is None
            or self._closed
        ):
            raise RuntimeError("control unavailable")
        result = self._groups.submit(group_id, patch, source=source)
        self._parameters_submitted(patch)
        return result

    def group_snapshot(self, group_id: str) -> GroupSnapshot:
        if self._groups is not None:
            return self._groups.snapshot(group_id)
        members = next(g.members for g in self.config.groups if g.id == group_id)
        return GroupSnapshot(
            any(self._desired.get(item).on for item in members),
            0,
            len(members),
            False,
            False,
        )

    def group_capabilities(self, group_id: str) -> LightCapabilities:
        """Return only ranges supported by every member of the group."""
        members = next(g.members for g in self.config.groups if g.id == group_id)
        found = [self.light_capabilities(item) for item in members]
        brightness = [c.brightness_range for c in found]
        mired = [c.mired_range for c in found]
        return LightCapabilities(_intersect(brightness), _intersect(mired))

    def pause(self) -> None:
        if self._closed:
            raise RuntimeError("runtime closed")
        if self.mode == ControlMode.PAUSED:
            return
        self._paused_from = self.mode
        self.set_mode(ControlMode.PAUSED)

    def resume(self) -> None:
        """Restore the pre-pause mode; old intent is not replayed or promoted."""
        if self._closed or self.mode != ControlMode.PAUSED:
            raise RuntimeError("runtime is not paused")
        if self._paused_from == ControlMode.ACTIVE:
            self.set_mode(ControlMode.ACTIVE)
        else:
            self.set_mode(ControlMode.OBSERVE)

    async def resync(self) -> None:
        """Forget evidence and re-read the catalog; never sends device commands."""
        if self._closed:
            raise RuntimeError("runtime closed")
        if self.mode == ControlMode.ACTIVE:
            self.set_mode(ControlMode.OBSERVE)
        elif self.mode == ControlMode.PAUSED:
            self._paused_from = ControlMode.OBSERVE
        if self._groups is not None:
            await self._groups.close()
            self._groups = None
        if self._engine is not None:
            await self._engine.close()
            self._engine = None
        self._stale = False
        self._observations.invalidate_bridge()
        self._announce(frozenset(light.id for light in self.config.lights))
        await self.adapter.resync()

    def diagnostics(self, light_id: str | None = None) -> dict[str, object]:
        """Return a redacted export; identifiers are replaced by stable aliases."""
        lights = list(self.config.lights)
        if light_id is not None and all(item.id != light_id for item in lights):
            raise KeyError(light_id)
        redact = self._redactor()
        data: dict[str, object] = {
            "runtime_version": RUNTIME_VERSION,
            "mode": self.mode.value,
            "report_extension_enabled": self.config.report_extension,
            "report_extension_ready": bool(
                getattr(getattr(self.adapter, "reports", None), "ready", False)
            ),
            "topology_stale": self._stale,
            "bridge_online": self._observations.bridge_online,
            "control_notice": CONTROL_NOTICE,
            "preferences_error": redact(self.preferences_error),
            "adapter_error": redact(getattr(self.adapter, "last_error", None)),
            "lights": {},
            "groups": {},
        }
        snapshot = self.metrics.snapshot()
        data["counters"] = snapshot["counters"]
        data["boot_samples"] = snapshot["boot"]
        for index, light in enumerate(self.config.lights, start=1):
            if light_id is not None and light.id != light_id:
                continue
            state = self.snapshot(light.id)
            observed = self._observations.snapshot(light.id)
            data["lights"][f"light_{index}"] = {
                "phase": state.phase.value,
                "reason": redact(state.reason),
                "effective_state": state.effective.value,
                "confirmed": state.confirmed,
                "available": state.available,
                "desired": {
                    "on": state.desired.on,
                    "brightness": state.desired.brightness,
                    "color_temp_kelvin": state.desired.color_temp_kelvin,
                    "generation": state.desired.generation,
                    "source": redact(state.desired.source),
                },
                "observed": {
                    "relay_state": observed.relay_state.value,
                    "relay_quality": observed.relay_quality.value,
                    "relay_retained": observed.relay_retained,
                    "led_state": observed.led_state.value,
                    "led_quality": observed.led_quality.value,
                    "led_retained": observed.led_retained,
                    "brightness": observed.brightness,
                    "color_temp_kelvin": observed.color_temp_kelvin,
                    "power_epoch": observed.power_epoch,
                    "bridge_epoch": observed.bridge_epoch,
                },
            }
        if light_id is None:
            for index, group in enumerate(self.config.groups, start=1):
                group_state = self.group_snapshot(group.id)
                data["groups"][f"group_{index}"] = {
                    "on": group_state.on,
                    "confirmed_members": group_state.confirmed_members,
                    "member_count": group_state.member_count,
                    "confirmed": group_state.confirmed,
                }
        return data

    def _redactor(self) -> Callable[[str | None], str | None]:
        """Build a scrubber for installation identifiers inside free text."""
        names: set[str] = {self.config.base_topic}
        for item in (*self.config.relay_channels, *self.config.lights):
            names.add(item.device_ieee)
            names.add(item.device_ieee.lower())
        names.update(channel.id for channel in self.config.relay_channels)
        names.update(light.id for light in self.config.lights)
        names.update(group.id for group in self.config.groups)
        names.update(room.id for room in self.config.rooms)
        topology = getattr(self.adapter, "topology", None)
        if topology is not None:
            for device in topology.devices():
                names.add(device.ieee)
                names.add(device.topic)
                names.add(device.topic.split("/", 1)[-1])
            for _group_id, group in topology.groups():
                names.add(group.topic)
                names.add(group.topic.split("/", 1)[-1])
        ordered = sorted((n for n in names if n), key=len, reverse=True)
        if not ordered:
            return lambda value: value
        pattern = re.compile(
            "(?<![A-Za-z0-9_])("
            + "|".join(re.escape(n) for n in ordered)
            + ")(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )

        def redact(value: str | None) -> str | None:
            return None if value is None else pattern.sub("[redacted]", value)

        return redact

    def snapshot(self, light_id: str) -> LightSnapshot:
        if self._engine is not None:
            return self._engine.snapshot(light_id)
        return LightSnapshot(
            self._desired.get(light_id),
            Phase.OFF,
            EffectiveState.UNKNOWN,
            False,
            False,
            None,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.adapter.set_active(False)
        if self._remove_topology:
            self._remove_topology()
        if self._groups:
            await self._groups.close()
        if self._engine:
            await self._engine.close()
        await self.adapter.close()
        self._subscribers.clear()
