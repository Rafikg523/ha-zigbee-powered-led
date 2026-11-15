"""Coordinate the HA MQTT adapter and core.

Control turns on once the bridge and device catalog are available.
A missing device disables only that circuit.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import replace

from .configuration import circuit_label
from .const import RUNTIME_VERSION
from .desired_store import DesiredStateStore
from .group_coordinator import GroupCoordinator
from .group_provisioner import (
    CatalogGroup,
    LightEndpoint,
    next_group_action,
    options_confirmed,
    z2m_group_name,
)
from .light_capabilities import LightCapabilities
from .metrics import Metrics
from .models import (
    BridgeObservation,
    ControlMode,
    EffectiveState,
    EvidenceQuality,
    GroupSnapshot,
    IntentPatch,
    LightSnapshot,
    Phase,
    RelayObservation,
    SwitchState,
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

    def __init__(
        self,
        config,
        adapter,
        *,
        user_inactive: bool = False,
        management_pending: str | None = None,
        management_members: tuple[str, ...] = (),
    ) -> None:
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
        self._signatures: dict[str, tuple | None] = {}
        self._absent: set[str] = set()
        self._absent_groups: set[str] = set()
        self._user_inactive = user_inactive
        self._management_pending = management_pending
        self._management_extra_members = set(management_members)
        self._management_lock = asyncio.Lock()
        self._language = "en"
        self.notices: dict[str, str] = {}
        self._notice_listener: Callable[[str, str | None], None] | None = None
        self._closed = False
        self._subscribers: set[Callable[[frozenset[str]], None]] = set()
        self._remove_topology = None
        self.group_error: str | None = None
        self._group_id_listener: Callable[[str, int], None] | None = None
        self._options_ready: set[str] = set()
        self._pending_groups: dict[str, int] = {}
        self._provision_task: asyncio.Task | None = None
        self._provision_again = False
        self._provision_backoff: dict[str, float] = {}
        self._provision_retry_task: asyncio.Task | None = None

    async def start(self) -> None:
        if hasattr(self.adapter, "power_hint_listener"):
            self.adapter.power_hint_listener = self._schedule_provision
        self._remove_topology = self.adapter.subscribe_topology(self._topology_changed)
        try:
            await self.adapter.start(self._observation)
        except BaseException:
            await self.close()
            raise

    def set_notice_listener(
        self, listener: Callable[[str, str | None], None] | None
    ) -> None:
        self._notice_listener = listener

    def set_language(self, language: str) -> None:
        self._language = language

    def _topology_changed(self, version: int) -> None:
        del version
        self._refresh_presence()
        self._announce(frozenset(light.id for light in self.config.lights))
        self._schedule_provision()

    def _catalog_seen(self) -> bool:
        seen = getattr(self.adapter, "catalog_seen", None)
        if seen is None:
            return bool(self.adapter.topology.devices())
        return bool(seen)

    def _light_present(self, light) -> bool:
        channel = next(
            (
                item
                for item in self.config.relay_channels
                if item.id == light.power_channel
            ),
            None,
        )
        if channel is None:
            return False
        narrowed = replace(
            self.config,
            lights=(light,),
            relay_channels=(channel,),
            groups=(),
            rooms=(),
        )
        try:
            validate_topology(narrowed, self.adapter.topology, require_groups=False)
        except TypeError, ValueError, KeyError:
            return False
        return True

    def _light_signature(self, light) -> tuple | None:
        if not self._light_present(light):
            return None
        topology = self.adapter.topology
        channel = next(
            item
            for item in self.config.relay_channels
            if item.id == light.power_channel
        )
        relay = topology.resolve_device(channel.device_ieee)
        led = topology.resolve_device(light.device_ieee)
        return (
            relay.topic,
            channel.endpoint,
            channel.state_key,
            led.topic,
            light.endpoint,
        )

    def _group_matches(self, group, absent: set[str]) -> bool:
        if self._management_pending in ("*", group.id):
            return False
        if any(member in absent for member in group.members):
            return False
        found = self.adapter.topology.group_named(z2m_group_name(group.name))
        if found is None:
            return False
        lights = {item.id: item for item in self.config.lights}
        expected = frozenset(
            (lights[item].device_ieee.lower(), lights[item].endpoint)
            for item in group.members
        )
        return found[1].members == expected

    def _publish_notice(self, key: str, message: str | None) -> None:
        if message is None:
            if key not in self.notices:
                return
            del self.notices[key]
        elif self.notices.get(key) == message:
            return
        else:
            self.notices[key] = message
        if self._notice_listener is not None:
            self._notice_listener(key, message)

    def _notice_text(self, kind: str, label: str) -> str:
        polish = str(self._language).lower().startswith("pl")
        if kind == "light":
            if polish:
                return (
                    f"Obwód {label} zniknął z katalogu Zigbee2MQTT. "
                    "Pozostałe obwody działają dalej."
                )
            return (
                f"Circuit {label} is missing from the Zigbee2MQTT catalog. "
                "The other circuits keep working."
            )
        if kind == "group":
            if polish:
                return (
                    f"Grupa {label} nie ma w Zigbee2MQTT oczekiwanego składu. "
                    "Jej obecne obwody nadal działają osobno."
                )
            return (
                f"Group {label} does not have the expected membership in "
                "Zigbee2MQTT. Its circuits that are still in the catalog keep "
                "working on their own."
            )
        if polish:
            return (
                "Żaden skonfigurowany obwód nie jest w katalogu Zigbee2MQTT. "
                "Sterowanie jest wyłączone."
            )
        return (
            "None of the configured circuits are in the Zigbee2MQTT catalog. "
            "Control is off."
        )

    def _refresh_presence(self) -> None:
        if not self._catalog_seen():
            self._maybe_arm()
            return
        absent = {
            light.id for light in self.config.lights if not self._light_present(light)
        }
        absent_groups: set[str] = set()
        if getattr(self.adapter.topology, "groups_seen", False):
            absent_groups = {
                group.id
                for group in self.config.groups
                if not self._group_matches(group, absent)
            }
        self._apply_presence(absent, absent_groups)
        self._maybe_arm()

    def _apply_presence(self, absent: set[str], absent_groups: set[str]) -> None:
        for light in self.config.lights:
            key = f"light_{light.id}"
            if light.id in absent:
                self._publish_notice(
                    key,
                    self._notice_text(
                        "light", circuit_label(light.id, getattr(light, "name", None))
                    ),
                )
            else:
                self._publish_notice(key, None)
        for group in self.config.groups:
            key = f"group_{group.id}"
            if group.id in absent_groups:
                self._publish_notice(key, self._notice_text("group", group.name))
            else:
                self._publish_notice(key, None)
        configured = {light.id for light in self.config.lights}
        if configured and absent == configured:
            self._publish_notice("catalog", self._notice_text("catalog", ""))
        else:
            self._publish_notice("catalog", None)
        if self._engine is not None:
            for light in self.config.lights:
                signature = None if light.id in absent else self._light_signature(light)
                previous = self._signatures.get(light.id)
                if light.id in absent:
                    self._engine.isolate(light.id)
                elif light.id not in self._engine.circuit_ids:
                    circuit = self._circuit_for(light)
                    if circuit is not None:
                        self._engine.attach(circuit)
                        self._apply_capabilities(light)
                elif previous is not None and signature != previous:
                    circuit = self._circuit_for(light)
                    if circuit is not None:
                        self._engine.replace_circuit(circuit)
                        self._apply_capabilities(light)
                self._signatures[light.id] = signature
            self._engine.set_blocked(frozenset(absent))
            for group in self.config.groups:
                if group.id in absent_groups:
                    self._engine.forget_group(group.id)
                else:
                    native = self._group_circuit(group)
                    if native is not None:
                        self._engine.remember_group(native)
        else:
            for light in self.config.lights:
                self._signatures[light.id] = (
                    None if light.id in absent else self._light_signature(light)
                )
        self._absent = set(absent)
        self._absent_groups = set(absent_groups)

    def _maybe_arm(self) -> None:
        if self._closed or self._user_inactive or self.mode == ControlMode.PAUSED:
            return
        configured = {light.id for light in self.config.lights}
        if self.mode == ControlMode.ACTIVE:
            if configured and self._absent == configured:
                self.set_mode(ControlMode.OBSERVE)
            return
        if not self._catalog_seen():
            return
        bridge = getattr(self.adapter, "bridge_online", None)
        if self._observations.bridge_online is not True and bridge is not True:
            return
        reports = getattr(self.adapter, "reports", None)
        if self.config.report_extension and reports is not None and not reports.ready:
            return
        if configured and self._absent == configured:
            return
        try:
            self.set_mode(ControlMode.ACTIVE)
        except RuntimeError, ValueError, TypeError:
            return

    def _observation(self, observation) -> None:
        delta = self._observations.ingest(observation)
        if isinstance(observation, BridgeObservation) and not observation.online:
            self._options_ready.clear()
            self.mode = ControlMode.OBSERVE
            self.adapter.set_active(False)
            if self._engine is not None:
                self._engine.set_enabled(False)
        if self._engine is not None:
            self._engine.notify(delta)
        else:
            self._announce(delta.light_ids)
        if isinstance(observation, BridgeObservation) and observation.online:
            self._maybe_arm()
            self._schedule_provision()
        if isinstance(observation, RelayObservation) and self._relay_powered(
            observation.channel_id
        ):
            self._schedule_provision()

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

    def _circuit_for(self, light) -> Circuit | None:
        if not self._light_present(light):
            return None
        channel = next(
            item
            for item in self.config.relay_channels
            if item.id == light.power_channel
        )
        relay = self.adapter.topology.resolve_device(channel.device_ieee)
        led = self.adapter.topology.resolve_device(light.device_ieee)
        return Circuit(
            light.id,
            channel.id,
            relay.topic + "/set",
            led.topic + "/set",
            f"{self.config.base_topic}:{channel.device_ieee}:{channel.endpoint}:{channel.state_key}",
            f"{self.config.base_topic}:{light.device_ieee}:{light.endpoint}",
            channel.state_key,
        )

    def _group_circuit(self, group) -> GroupCircuit | None:
        if not self._group_matches(group, self._absent):
            return None
        found = self.adapter.topology.group_named(z2m_group_name(group.name))
        if found is None:
            return None
        native_id, native = found
        return GroupCircuit(
            group.id,
            native.topic + "/set",
            f"{self.config.base_topic}:group:{native_id}",
            group.members,
        )

    def _apply_capabilities(self, light) -> None:
        if self._engine is None:
            return
        try:
            device = self.adapter.topology.resolve_device(light.device_ieee)
            capabilities = LightCapabilities.from_exposes(
                device.capabilities, endpoint=device.endpoint_name(light.endpoint)
            )
        except KeyError, ValueError, TypeError:
            capabilities = LightCapabilities(None, None)
        self._engine.set_capabilities(light.id, capabilities)

    def _make_engine(self) -> Reconciler:
        circuits = []
        for light in self.config.lights:
            if light.id in self._absent:
                continue
            circuit = self._circuit_for(light)
            if circuit is not None:
                circuits.append(circuit)
        present = {item.light_id for item in circuits}
        groups = []
        for group in self.config.groups:
            if not set(group.members) <= present:
                continue
            native = self._group_circuit(group)
            if native is not None:
                groups.append(native)
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
            members={group.id: group.members for group in self.config.groups},
        )
        for light in self.config.lights:
            if light.id in {item.light_id for item in circuits}:
                try:
                    device = self.adapter.topology.resolve_device(light.device_ieee)
                    capabilities = LightCapabilities.from_exposes(
                        device.capabilities,
                        endpoint=device.endpoint_name(light.endpoint),
                    )
                except KeyError, ValueError, TypeError:
                    capabilities = LightCapabilities(None, None)
                engine.set_capabilities(light.id, capabilities)
        engine.subscribe(self._announce)
        return engine

    def set_mode(self, mode: ControlMode, *, manual: bool = False) -> None:
        if self._closed or not isinstance(mode, ControlMode):
            raise RuntimeError("invalid or closed runtime")
        if manual:
            self._user_inactive = mode != ControlMode.ACTIVE
        if mode == ControlMode.ACTIVE:
            bridge = getattr(self.adapter, "bridge_online", None)
            if self._observations.bridge_online is False or bridge is False:
                raise RuntimeError("bridge offline")
            reports = getattr(self.adapter, "reports", None)
            if (
                self.config.report_extension
                and reports is not None
                and not reports.ready
            ):
                raise RuntimeError(
                    "report extension unavailable; resync after loading it"
                )
            if self._catalog_seen():
                self._absent = {
                    light.id
                    for light in self.config.lights
                    if not self._light_present(light)
                }
            configured = {light.id for light in self.config.lights}
            if configured and self._catalog_seen() and self._absent == configured:
                raise RuntimeError("configured devices missing")
            created = self._engine is None
            engine = self._make_engine() if created else self._engine
            assert engine is not None
            try:
                self.adapter.set_active(True)
                engine.set_enabled(True)
            except Exception:
                self.adapter.set_active(False)
                if created:
                    engine.set_enabled(False)
                    self._groups = None
                raise
            if created:
                self._engine = engine
                engine.set_blocked(frozenset(self._absent))
        else:
            if self._engine is not None:
                self._engine.set_enabled(False)
            self.adapter.set_active(False)
        self.mode = mode
        self._announce(frozenset(light.id for light in self.config.lights))
        if mode == ControlMode.OBSERVE:
            self._schedule_provision()

    def submit(self, light_ids: tuple[str, ...], patch: IntentPatch, *, source: str):
        if self.mode != ControlMode.ACTIVE or self._engine is None or self._closed:
            raise RuntimeError("control unavailable")
        if self._absent.intersection(light_ids):
            raise RuntimeError("device absent")
        if self._management_members().intersection(light_ids):
            raise RuntimeError("group management pending")
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

    def set_group_id_listener(
        self, listener: Callable[[str, int], None] | None
    ) -> None:
        """Persist a discovered Zigbee group id. The listener must not publish."""
        self._group_id_listener = listener

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
        if (
            self._management_pending in ("*", group_id)
            or group_id in self._absent_groups
        ):
            raise RuntimeError("group unavailable")
        result = self._groups.submit(group_id, patch, source=source)
        self._parameters_submitted(patch)
        return result

    def group_snapshot(self, group_id: str) -> GroupSnapshot:
        members = next(g.members for g in self.config.groups if g.id == group_id)
        if (
            self._management_pending in ("*", group_id)
            or group_id in self._absent_groups
        ):
            return GroupSnapshot(False, 0, len(members), False, False)
        if (
            self._groups is not None
            and self._engine is not None
            and group_id in self._engine.groups
        ):
            return self._groups.snapshot(group_id)
        if self._engine is not None:
            snapshots = [self.snapshot(item) for item in members]
            confirmed = sum(1 for item in snapshots if item.confirmed)
            return GroupSnapshot(
                any(item.desired.on for item in snapshots),
                confirmed,
                len(members),
                confirmed == len(members),
                all(item.available for item in snapshots),
            )
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
        self._signatures.clear()
        self._observations.invalidate_bridge()
        self._announce(frozenset(light.id for light in self.config.lights))
        self._options_ready.clear()
        self._pending_groups.clear()
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
            "group_error": redact(self.group_error),
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
        names.update(light.name for light in self.config.lights)
        names.update(group.id for group in self.config.groups)
        names.update(group.name for group in self.config.groups)
        names.update(z2m_group_name(group.name) for group in self.config.groups)
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
        if self._engine is not None and light_id in self._engine.circuit_ids:
            state = self._engine.snapshot(light_id)
            if light_id in self._absent:
                return replace(state, available=False, reason="device_absent")
            if light_id in self._management_members():
                return replace(
                    state, available=False, reason="group_management_pending"
                )
            return state
        return LightSnapshot(
            self._desired.get(light_id),
            Phase.ERROR if light_id in self._absent else Phase.OFF,
            EffectiveState.UNKNOWN,
            False,
            False,
            "device_absent" if light_id in self._absent else None,
        )

    def _management_members(self) -> set[str]:
        return self._management_extra_members | {
            member
            for group in self.config.groups
            if self._management_pending in ("*", group.id)
            for member in group.members
        }

    def _relay_powered(self, channel_id: str) -> bool:
        hints = getattr(self.adapter, "relay_power_hints", {})
        if channel_id in hints:
            return hints[channel_id] == SwitchState.ON
        for light in self.config.lights:
            if light.power_channel != channel_id:
                continue
            observed = self._observations.snapshot(light.id)
            # A live MQTT hint permits membership maintenance, never LED /set.
            if (
                observed.relay_state == SwitchState.ON
                and observed.relay_retained is False
                and observed.relay_quality != EvidenceQuality.CACHE
            ):
                return True
        return False

    def _powered_lights(self) -> frozenset[str]:
        return frozenset(
            light.id
            for light in self.config.lights
            if self._relay_powered(light.power_channel)
        )

    def _schedule_provision(self) -> None:
        if (
            self._closed
            or self._management_pending
            or self.mode == ControlMode.PAUSED
            or not self.config.groups
        ):
            return
        if self._provision_task is not None and not self._provision_task.done():
            self._provision_again = True
            return
        self._provision_task = asyncio.create_task(self._run_provision())

    async def _run_provision(self) -> None:
        try:
            await self._provision_step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - provisioning must fail closed
            self.group_error = str(exc)
        finally:
            self._provision_task = None
            if (
                self._provision_again
                and not self._closed
                and self.mode != ControlMode.PAUSED
            ):
                self._provision_again = False
                self._schedule_provision()

    def _plan_action(self):
        if not getattr(self.adapter.topology, "groups_seen", False):
            return None
        directory: dict[str, LightEndpoint] = {}
        for light in self.config.lights:
            try:
                device = self.adapter.topology.resolve_device(light.device_ieee)
            except KeyError:
                continue
            directory[light.id] = LightEndpoint(
                light.device_ieee.lower(), light.endpoint, device.name
            )
        catalog = tuple(
            CatalogGroup(group_id, group.name, group.members)
            for group_id, group in self.adapter.topology.groups()
        )
        present = {item.name for item in catalog}
        for name in tuple(self._pending_groups):
            if name in present:
                del self._pending_groups[name]
        return next_group_action(
            tuple(
                group
                for group in self.config.groups
                if self._provision_backoff.get(z2m_group_name(group.name), 0)
                <= self._clock.now()
            ),
            directory,
            catalog,
            self._powered_lights(),
            frozenset(self._options_ready),
            self._pending_groups,
        )

    def _defer_provision(self, name: str) -> None:
        """Retry metadata maintenance after backoff, allowing other groups through."""
        self._provision_backoff[name] = self._clock.now() + 10
        self._provision_again = True
        if self._provision_retry_task is None or self._provision_retry_task.done():
            self._provision_retry_task = asyncio.create_task(self._retry_provision())

    async def _retry_provision(self) -> None:
        while self._provision_backoff and not self._closed:
            await self._clock.sleep(
                max(0, min(self._provision_backoff.values()) - self._clock.now())
            )
            self._provision_backoff = {
                name: deadline
                for name, deadline in self._provision_backoff.items()
                if deadline > self._clock.now()
            }
            self._schedule_provision()

    def _sync_group_ids(self) -> None:
        updated = []
        changed = False
        for group in self.config.groups:
            found = self.adapter.topology.group_named(z2m_group_name(group.name))
            if found is None or found[0] == group.z2m_id:
                updated.append(group)
                continue
            changed = True
            updated.append(replace(group, z2m_id=found[0]))
            if self._group_id_listener is not None:
                self._group_id_listener(group.id, found[0])
        if changed:
            self.config = replace(self.config, groups=tuple(updated))

    async def _provision_step(self) -> None:
        action = self._plan_action()
        if action is None:
            self._sync_group_ids()
            self.group_error = None
            return
        request_bridge = getattr(self.adapter, "request_bridge", None)
        if request_bridge is None:
            return
        name = str(
            action.payload.get(
                "group", action.payload.get("friendly_name", action.payload.get("id"))
            )
        )
        version = self.adapter.topology.version
        request = asyncio.create_task(request_bridge(action.path, action.payload))
        timer = asyncio.create_task(self._clock.sleep(10))
        try:
            done, _pending = await asyncio.wait(
                {request, timer}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not timer.done():
                timer.cancel()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, timer, return_exceptions=True)
        if request.cancelled() or request not in done:
            self.group_error = "bridge request timed out"
            self._defer_provision(name)
            return
        try:
            response = request.result()
        except RuntimeError, ValueError, TypeError:
            self._defer_provision(name)
            raise
        if not isinstance(response, dict) or response.get("status") != "ok":
            self.group_error = "bridge request rejected"
            self._defer_provision(name)
            return
        data = response.get("data")
        advanced = False
        if action.path == "group/add" and isinstance(data, dict):
            friendly = data.get("friendly_name")
            assigned = data.get("id")
            if isinstance(friendly, str) and type(assigned) is int:
                self._pending_groups[friendly] = assigned
                self._options_ready.discard(friendly)
                advanced = True
        if action.path == "group/options" and options_confirmed(response):
            group_name = action.payload.get("id")
            if isinstance(group_name, str):
                self._options_ready.add(group_name)
                advanced = True
        if action.path in {"group/members/add", "group/members/remove"}:
            # The republished group catalog is the source of truth for membership.
            if self.adapter.topology.version == version:
                self._defer_provision(name)
            else:
                self._provision_again = True
            self.group_error = None
            return
        if not advanced:
            self.group_error = "bridge request rejected"
            self._defer_provision(name)
            return
        self.group_error = None
        self._provision_again = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.adapter.set_active(False)
        retry = self._provision_retry_task
        if retry is not None and not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        task = self._provision_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._remove_topology:
            self._remove_topology()
        if self._groups:
            await self._groups.close()
        if self._engine:
            await self._engine.close()
        await self.adapter.close()
        self._subscribers.clear()
