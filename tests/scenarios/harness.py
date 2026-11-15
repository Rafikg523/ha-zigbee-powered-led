"""Whole-entry harness: real config entry, runtime and MQTT adapter over fake HA MQTT.

Only the clock and the evidence-source classification are synthetic. The adapter
cannot prove that a non-retained Zigbee2MQTT message is a genuine device report, so
by default `TrustingAdapter` upgrades such messages to DEVICE_REPORT to model an
adapter that has that evidence. `trust_reports=False` keeps the production
classification, under which no state can ever be confirmed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.zigbee_led_orchestrator.const import DOMAIN
from custom_components.zigbee_led_orchestrator.models import (
    ControlMode,
    EvidenceQuality,
    LedObservation,
    RelayObservation,
)
from tests.helpers.clock import FakeClock
from tests.unit.test_configuration import valid_config

FIXTURE = Path(__file__).parents[1] / "fixtures" / "z2m" / "pilot_synthetic.json"
RELAY = "zigbee2mqtt/room/relay"
LED = {"light_a": "zigbee2mqtt/room/led1", "light_b": "zigbee2mqtt/room/led2"}
GROUP = "zigbee2mqtt/group_a_orchestrator"


def load_pilot() -> dict:
    return json.loads(FIXTURE.read_text())


class ScenarioClock(FakeClock):
    """FakeClock that tolerates Home Assistant's own unrelated loop timers."""

    @staticmethod
    async def _settle() -> None:
        loop = asyncio.get_running_loop()
        ready = loop._ready
        for _ in range(1000):
            await asyncio.sleep(0)
            if not ready:
                await asyncio.sleep(0)
                if not ready:
                    return
        raise RuntimeError("ScenarioClock could not settle the event loop")


def trusting_adapter_class(base):
    class TrustingAdapter(base):
        trust_reports = True

        def _emit(self, observation) -> None:
            if (
                self.trust_reports
                and isinstance(observation, (RelayObservation, LedObservation))
                and not observation.retained
                and observation.quality == EvidenceQuality.UNKNOWN_SOURCE
            ):
                observation = replace(
                    observation, quality=EvidenceQuality.DEVICE_REPORT
                )
            super()._emit(observation)

    return TrustingAdapter


class Rig:
    def __init__(self, hass, mqtt_mock, clock, adapters) -> None:
        self.hass = hass
        self.mqtt_mock = mqtt_mock
        self.clock = clock
        self.adapters = adapters
        self.entry: MockConfigEntry | None = None
        self.pilot = load_pilot()

    @property
    def runtime(self):
        return self.entry.runtime_data

    async def setup(self, config=None):
        self.entry = MockConfigEntry(domain=DOMAIN, data=config or valid_config())
        self.entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.settle()
        return self.entry

    async def reload(self) -> None:
        assert await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.settle()

    async def settle(self) -> None:
        await self.hass.async_block_till_done()
        await self.clock.advance(0)

    async def advance(self, seconds: float) -> None:
        await self.clock.advance(seconds)
        await self.hass.async_block_till_done()

    def fire(self, topic: str, payload, *, retain: bool = False) -> None:
        async_fire_mqtt_message(self.hass, topic, json.dumps(payload), retain=retain)

    async def publish_catalog(self, *, retain: bool = True) -> None:
        self.fire("zigbee2mqtt/bridge/state", {"state": "online"}, retain=retain)
        await self.settle()
        self.fire("zigbee2mqtt/bridge/devices", self.pilot["devices"], retain=retain)
        await self.settle()
        self.fire("zigbee2mqtt/bridge/groups", self.pilot["groups"], retain=retain)
        await self.settle()

    async def report_relay(self, l1: str, l2: str = "OFF", *, retain=False) -> None:
        self.fire(RELAY, {"state_l1": l1, "state_l2": l2}, retain=retain)
        await self.settle()

    async def report_led(
        self,
        light: str,
        state: str = "ON",
        *,
        brightness: int | None = None,
        mired: int | None = None,
        retain: bool = False,
    ) -> None:
        payload: dict[str, object] = {"state": state}
        if brightness is not None:
            payload["brightness"] = brightness
        if mired is not None:
            payload["color_temp"] = mired
        self.fire(LED[light], payload, retain=retain)
        await self.settle()

    async def set_connection(self, connected: bool) -> None:
        """Change HA's MQTT connection state and notify the adapter's callback."""
        self.hass.data["mqtt"].client.connected = connected
        for callback in tuple(self.connection_callbacks):
            callback(connected)
        await self.settle()

    def entity_id(self, suffix: str) -> str:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            if entry.unique_id == f"{self.entry.entry_id}_{suffix}":
                return entry.entity_id
        raise KeyError(suffix)

    def state(self, suffix: str):
        return self.hass.states.get(self.entity_id(suffix))

    async def light(self, service: str, suffix: str, **data) -> None:
        await self.hass.services.async_call(
            "light",
            service,
            {"entity_id": self.entity_id(suffix), **data},
            blocking=True,
        )
        await self.settle()

    async def service(self, name: str, **data):
        return await self.hass.services.async_call(
            DOMAIN,
            name,
            {"config_entry_id": self.entry.entry_id, **data},
            blocking=True,
            return_response=name == "diagnose",
        )

    def _publications(self) -> list[tuple[str, dict]]:
        result = []
        for call in self.mqtt_mock.async_publish.call_args_list:
            topic, payload, _qos, retain = call.args[:4]
            assert retain is False, f"retained command to {topic}"
            result.append((topic, json.loads(payload)))
        return result

    def publications(self) -> list[tuple[str, dict]]:
        """Return device and report-session publications, never bridge administration."""
        return [
            item for item in self._publications() if "/bridge/request/" not in item[0]
        ]

    def admin_publications(self) -> list[tuple[str, dict]]:
        """Return non-retained Zigbee2MQTT bridge requests."""
        return [item for item in self._publications() if "/bridge/request/" in item[0]]

    def diagnostics(self) -> dict:
        return self.runtime.diagnostics()

    def phase(self, light_id: str) -> str:
        return self.runtime.snapshot(light_id).phase.value

    async def boot_to_off(self) -> None:
        """Catalog and OFF reports leave control on and publish nothing."""
        await self.publish_catalog()
        await self.report_relay("OFF", "OFF")
        await self.advance(1)
        assert self.publications() == []
        assert self.runtime.mode == ControlMode.ACTIVE

    async def unload(self) -> None:
        if self.entry is not None and self.entry.state.value == "loaded":
            await self.hass.config_entries.async_unload(self.entry.entry_id)
        mqtt_entries = self.hass.config_entries.async_entries("mqtt")
        if mqtt_entries:
            await self.hass.config_entries.async_unload(mqtt_entries[0].entry_id)
        timer = getattr(self.mqtt_mock._mock_wraps, "_misc_timer", None)
        if timer is not None:
            timer.cancel()
