"""Bridge HA MQTT messages to conservative domain observations."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .device_reports import DeviceReports
from .group_provisioner import z2m_group_name
from .models import (
    BridgeObservation,
    EvidenceQuality,
    LedObservation,
    Observation,
    OrchestratorConfig,
    RelayObservation,
    SwitchState,
)
from .topology import Topology

_LOGGER = logging.getLogger(__name__)
_BRIDGE_PATHS = frozenset(
    {"group/add", "group/options", "group/members/add", "group/members/remove"}
)


class MqttAdapter:
    """Use the HA MQTT client, fencing callbacks and failing closed on changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: OrchestratorConfig,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hass = hass
        self._config = config
        self._now = now
        self.topology = Topology(config.base_topic)
        self.reports = DeviceReports(config)
        self._session = 0
        self._subscriptions: dict[str, Callable[[], None]] = {}
        self._status_unsubscribe: Callable[[], None] | None = None
        self._callback: Callable[[Observation], None] | None = None
        self._connected = False
        self._bridge_online = False
        self._active = False
        self._closed = True
        self._updating = 0
        self._devices_revision = 0
        self._have_devices = False
        self._pending_groups: object | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._topology_subscribers: set[Callable[[int], None]] = set()
        self._bridge_waiters: dict[str, asyncio.Future] = {}
        self.last_error: str | None = None

    async def start(self, on_observation: Callable[[Observation], None]) -> None:
        if not self._closed:
            raise RuntimeError("adapter already started")
        self._closed = False
        self._callback = on_observation
        self._connected = mqtt.is_connected(self._hass)
        self._status_unsubscribe = mqtt.async_subscribe_connection_status(
            self._hass, self._connection_changed
        )
        try:
            if self._config.report_extension:
                await self._subscribe(f"{self._config.base_topic}/orchestrator/report")
            for suffix in ("state", "devices", "groups", "event", "response/group/#"):
                await self._subscribe(f"{self._config.base_topic}/bridge/{suffix}")
        except Exception:
            await self.close()
            raise

    def subscribe_topology(self, callback: Callable[[int], None]) -> Callable[[], None]:
        """Notify consumers synchronously when catalog validity changes."""
        self._topology_subscribers.add(callback)
        return lambda: self._topology_subscribers.discard(callback)

    def _topology_changed(self) -> None:
        for callback in tuple(self._topology_subscribers):
            callback(self.topology.version)

    @property
    def catalog_seen(self) -> bool:
        return self._have_devices

    @property
    def bridge_online(self) -> bool:
        return self._bridge_online

    def set_active(self, active: bool) -> None:
        """Explicitly authorize new commands; changing mode never publishes."""
        if active and (self._closed or not self._connected or not self._bridge_online):
            raise RuntimeError("control unavailable")
        if active and self._config.report_extension and not self.reports.ready:
            raise RuntimeError("report extension unavailable; resync after loading it")
        self._active = active

    async def _subscribe(self, topic: str, revision: int | None = None) -> None:
        session = self._session

        async def receive(message: mqtt.ReceiveMessage) -> None:
            if (
                self._closed
                or session != self._session
                or (revision is not None and revision != self._devices_revision)
            ):
                return
            await self._receive(message, session)

        unsubscribe = await mqtt.async_subscribe(self._hass, topic, receive)
        if (
            self._closed
            or session != self._session
            or (revision is not None and revision != self._devices_revision)
        ):
            unsubscribe()
            return
        previous = self._subscriptions.get(topic)
        if previous is not None:
            previous()
        self._subscriptions[topic] = unsubscribe

    def _connection_changed(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self.reports.reset()
            self._session += 1
            self._bridge_online = False
            self._active = False
            self._fail_bridge_waiters()
            self.topology.update_devices([])
            self._topology_changed()
            self._have_devices = False
            self._pending_groups = None
            if self._callback:
                self._callback(
                    BridgeObservation(
                        False, self._now(), False, EvidenceQuality.UNKNOWN_SOURCE
                    )
                )
            for unsubscribe in self._subscriptions.values():
                unsubscribe()
            self._subscriptions.clear()
        elif not self._closed:
            self._schedule_resubscribe()

    async def resync(self) -> None:
        """Drop all evidence and subscriptions, then re-read retained catalogs.

        No device commands are published; an opted-in report metadata handshake
        may be sent. Device commands stay off until control is active again.
        """
        if self._closed:
            raise RuntimeError("adapter closed")
        self.last_error = None
        self._session += 1
        self.reports.reset()
        self._bridge_online = False
        self._active = False
        self._fail_bridge_waiters()
        self.topology.update_devices([])
        self._topology_changed()
        self._have_devices = False
        self._pending_groups = None
        for unsubscribe in self._subscriptions.values():
            unsubscribe()
        self._subscriptions.clear()
        if self._connected:
            await self._resubscribe()

    def _schedule_resubscribe(self) -> None:
        task = self._hass.async_create_task(self._resubscribe())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resubscribe(self) -> None:
        session = self._session
        if self._config.report_extension:
            await self._subscribe(f"{self._config.base_topic}/orchestrator/report")
        for suffix in ("state", "devices", "groups", "event", "response/group/#"):
            if self._closed or session != self._session:
                return
            await self._subscribe(f"{self._config.base_topic}/bridge/{suffix}")

    async def _receive(self, message: mqtt.ReceiveMessage, session: int) -> None:
        try:
            data = json.loads(
                message.payload,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("non-finite JSON")
                ),
            )
            prefix = f"{self._config.base_topic}/bridge/"
            if message.topic == prefix + "state":
                if not isinstance(data, dict) or data.get("state") not in (
                    "online",
                    "offline",
                ):
                    raise ValueError("invalid bridge state")
                was_online = self._bridge_online
                self._bridge_online = data["state"] == "online"
                if not self._bridge_online:
                    self.reports.reset()
                    self._active = False
                    self._fail_bridge_waiters()
                    self.topology.update_devices([])
                    self._topology_changed()
                    self._have_devices = False
                    self._pending_groups = None
                    if was_online:
                        self._session += 1
                        for unsubscribe in self._subscriptions.values():
                            unsubscribe()
                        self._subscriptions.clear()
                        if self._connected:
                            self._schedule_resubscribe()
                self._emit(
                    BridgeObservation(
                        self._bridge_online,
                        self._now(),
                        message.retain,
                        EvidenceQuality.CACHE
                        if message.retain
                        else EvidenceQuality.UNKNOWN_SOURCE,
                    )
                )
                if (
                    self._bridge_online
                    and not was_online
                    and self._config.report_extension
                ):
                    self.reports.reset()
                    await mqtt.async_publish(
                        self._hass,
                        f"{self._config.base_topic}/orchestrator/subscribe",
                        json.dumps(self.reports.request()),
                        retain=False,
                    )
            elif message.topic == f"{self._config.base_topic}/orchestrator/report":
                if self._config.report_extension and self._bridge_online:
                    was_broken = self.reports.broken
                    was_ready = self.reports.ready
                    for observation in self.reports.ingest(
                        data, retained=message.retain, now=self._now()
                    ):
                        self._emit(observation)
                    if self.reports.ready and not was_ready:
                        self._topology_changed()
                    if self.reports.broken and not was_broken:
                        self.last_error = "report_sequence_gap; resync required"
                        self._active = False
                        self._emit(
                            BridgeObservation(
                                False,
                                self._now(),
                                False,
                                EvidenceQuality.UNKNOWN_SOURCE,
                            )
                        )
            elif message.topic == prefix + "devices":
                self._updating += 1
                self._devices_revision += 1
                revision = self._devices_revision
                try:
                    self.topology.update_devices(data)
                    self._topology_changed()
                    first_devices = not self._have_devices
                    self._have_devices = True
                    if first_devices and self._pending_groups is not None:
                        pending, self._pending_groups = self._pending_groups, None
                        self.topology.update_groups(pending)
                        self._topology_changed()
                    for topic in list(self._subscriptions):
                        if (
                            not topic.startswith(prefix)
                            and topic
                            != f"{self._config.base_topic}/orchestrator/report"
                        ):
                            self._subscriptions.pop(topic)()
                    addresses = set()
                    for config in (*self._config.relay_channels, *self._config.lights):
                        try:
                            device = self.topology.resolve_device(config.device_ieee)
                        except KeyError:
                            continue
                        if config.endpoint in device.endpoints:
                            addresses.add(device.topic)
                    for topic in addresses:
                        if (
                            session != self._session
                            or self._closed
                            or revision != self._devices_revision
                        ):
                            return
                        await self._subscribe(topic, revision)
                finally:
                    self._updating -= 1
            elif message.topic == prefix + "groups":
                if not self._have_devices:
                    if not isinstance(data, list):
                        raise ValueError("invalid groups")
                    self._pending_groups = data
                else:
                    self.topology.update_groups(data)
                    self._topology_changed()
            elif message.topic.startswith(prefix + "response/group/"):
                self._finish_bridge_response(data)
            elif message.topic == prefix + "event":
                if not isinstance(data, dict) or not isinstance(data.get("type"), str):
                    raise ValueError("invalid event")
                if data["type"] in {"device_leave", "device_joined"}:
                    self._devices_revision += 1
                    self.topology.update_devices([])
                    self._topology_changed()
                    self._have_devices = False
                    self._pending_groups = None
            elif not message.topic.startswith(prefix):
                self._device_message(message, data)
        except (ValueError, TypeError, KeyError) as exc:
            if message.topic == f"{self._config.base_topic}/bridge/devices":
                self.topology.update_devices([])
                self._topology_changed()
                self._have_devices = False
                self._pending_groups = None
            elif message.topic == f"{self._config.base_topic}/bridge/groups":
                self.topology.update_groups([], catalog=False)
                self._topology_changed()
                self._pending_groups = None
            self.last_error = str(exc)
            _LOGGER.warning("Ignoring MQTT message: %s", exc)

    def _device_message(self, message: mqtt.ReceiveMessage, data: object) -> None:
        if self._config.report_extension:
            # Merged normal states cannot overwrite raw per-attribute evidence.
            return
        if not isinstance(data, dict):
            raise TypeError("device payload must be an object")
        quality = (
            EvidenceQuality.CACHE if message.retain else EvidenceQuality.UNKNOWN_SOURCE
        )
        for channel in self._config.relay_channels:
            try:
                device = self.topology.resolve_device(channel.device_ieee)
            except KeyError:
                continue
            if (
                device.topic == message.topic
                and channel.endpoint in device.endpoints
                and channel.state_key in data
            ):
                state = self._state(data[channel.state_key])
                self._emit(
                    RelayObservation(
                        channel.id, state, self._now(), message.retain, quality
                    )
                )
        for light in self._config.lights:
            try:
                device = self.topology.resolve_device(light.device_ieee)
            except KeyError:
                continue
            if (
                device.topic == message.topic
                and light.endpoint in device.endpoints
                and "state" in data
            ):
                state = self._state(data["state"])
                brightness = data.get("brightness")
                if brightness is not None and (
                    type(brightness) is not int or not 0 <= brightness <= 255
                ):
                    raise ValueError("invalid brightness")
                mired = data.get("color_temp")
                if mired is not None and (type(mired) is not int or mired <= 0):
                    raise ValueError("invalid color temperature")
                kelvin = round(1_000_000 / mired) if mired is not None else None
                if (
                    kelvin is not None
                    and not light.min_kelvin <= kelvin <= light.max_kelvin
                ):
                    raise ValueError("color temperature outside configured range")
                self._emit(
                    LedObservation(
                        light.id,
                        state,
                        self._now(),
                        message.retain,
                        quality,
                        brightness,
                        kelvin,
                    )
                )

    @staticmethod
    def _state(value: object) -> SwitchState:
        if value not in ("ON", "OFF"):
            raise ValueError("invalid state")
        return SwitchState(value)

    def _emit(self, observation: Observation) -> None:
        if self._callback is not None:
            self._callback(observation)

    def _fail_bridge_waiters(self) -> None:
        waiters = tuple(self._bridge_waiters.values())
        self._bridge_waiters.clear()
        for future in waiters:
            if not future.done():
                future.set_exception(RuntimeError("bridge request unavailable"))

    def _finish_bridge_response(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        transaction = data.get("transaction")
        future = self._bridge_waiters.get(transaction)
        if future is not None and not future.done():
            future.set_result(data)

    async def request_bridge(self, path: str, payload: dict[str, object]) -> dict:
        """Publish one non-retained bridge request and return its response.

        This is allowed while control is inactive. It never targets a device
        ``/set`` topic and never retains the request.
        """
        if path not in _BRIDGE_PATHS or type(payload) is not dict:
            raise ValueError("unsupported bridge request")
        if (
            self._closed
            or not self._connected
            or not self._bridge_online
            or self._updating
        ):
            raise RuntimeError("bridge request unavailable")
        transaction = secrets.token_hex(8)
        body = dict(payload)
        body["transaction"] = transaction
        encoded = json.dumps(body, separators=(",", ":"), allow_nan=False)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._bridge_waiters[transaction] = future
        try:
            await mqtt.async_publish(
                self._hass,
                f"{self._config.base_topic}/bridge/request/{path}",
                encoded,
                retain=False,
            )
            response = await future
        finally:
            self._bridge_waiters.pop(transaction, None)
        if not isinstance(response, dict):
            raise TypeError("invalid bridge response")
        return response

    async def publish(
        self, topic: str, payload: dict[str, object], *, retain: bool = False
    ) -> None:
        if retain:
            raise ValueError("retained commands are forbidden")
        if any(
            key.startswith("state") and value == "TOGGLE"
            for key, value in payload.items()
        ):
            raise ValueError("TOGGLE commands are forbidden")
        if (
            self._closed
            or not self._active
            or not self._connected
            or not self._bridge_online
            or self._updating
        ):
            raise RuntimeError("control unavailable")
        allowed = set()
        reads: dict[str, set[str]] = {}
        for channel in self._config.relay_channels:
            try:
                device = self.topology.resolve_device(channel.device_ieee)
                if channel.endpoint in device.endpoints:
                    reads.setdefault(device.topic + "/get", set()).add(
                        channel.state_key
                    )
            except KeyError:
                pass
        for light in self._config.lights:
            try:
                device = self.topology.resolve_device(light.device_ieee)
                if light.endpoint in device.endpoints:
                    reads.setdefault(device.topic + "/get", set()).update(
                        ("state", "brightness", "color_temp")
                    )
            except KeyError:
                pass
        if topic.endswith("/get"):
            if (
                not self._config.report_extension
                or not self.reports.ready
                or topic not in reads
            ):
                raise RuntimeError("unknown or stale read topic")
            if (
                not payload
                or not set(payload) <= reads[topic]
                or any(value != "" for value in payload.values())
            ):
                raise ValueError("invalid read payload")
            allowed.add(topic)
        for config in (*self._config.relay_channels, *self._config.lights):
            try:
                device = self.topology.resolve_device(config.device_ieee)
                if config.endpoint in device.endpoints:
                    allowed.add(device.topic + "/set")
            except KeyError:
                pass
        lights = {light.id: light for light in self._config.lights}
        for config in self._config.groups:
            found = self.topology.group_named(z2m_group_name(config.name))
            if found is None:
                continue
            expected = frozenset(
                (lights[member].device_ieee.lower(), lights[member].endpoint)
                for member in config.members
            )
            if found[1].members == expected:
                allowed.add(found[1].topic + "/set")
        if topic not in allowed:
            raise RuntimeError("unknown or stale command topic")
        affected = set()
        for light in self._config.lights:
            try:
                if (
                    self.topology.resolve_device(light.device_ieee).topic + "/set"
                    == topic
                ):
                    affected.add(light.id)
            except KeyError:
                continue
        for group in self._config.groups:
            found = self.topology.group_named(z2m_group_name(group.name))
            if found is not None and found[1].topic + "/set" == topic:
                affected.update(group.members)
        try:
            await mqtt.async_publish(
                self._hass,
                topic,
                json.dumps(payload, separators=(",", ":"), allow_nan=False),
                retain=False,
            )
        finally:
            # Reports that arrived before completion cannot confirm this send.
            self.reports.invalidate_leds(tuple(affected))

    async def close(self) -> None:
        self._closed = True
        self._session += 1
        self.reports.reset()
        self._active = False
        self._fail_bridge_waiters()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for unsubscribe in self._subscriptions.values():
            unsubscribe()
        self._subscriptions.clear()
        if self._status_unsubscribe:
            self._status_unsubscribe()
            self._status_unsubscribe = None
        self._callback = None
        self._topology_subscribers.clear()
