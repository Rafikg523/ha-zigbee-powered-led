"""Light entity behavior using synthetic runtime snapshots."""

from dataclasses import replace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zigbee_led_orchestrator.models import (
    DesiredState,
    EffectiveState,
    LightSnapshot,
    Phase,
)


class RuntimeStub:
    def __init__(self):
        self.desired = DesiredState(False, None, None, 0, "initial", 0.0)
        self.available = True
        self.patches = []

    def snapshot(self, light_id):
        return LightSnapshot(
            self.desired,
            Phase.POWERING,
            EffectiveState.UNKNOWN,
            False,
            self.available,
            None,
        )

    def submit(self, light_ids, patch, *, source):
        if not self.available:
            raise RuntimeError("control unavailable")
        self.patches.append(patch)
        self.desired = replace(
            self.desired,
            on=self.desired.on if patch.on is None else patch.on,
            brightness=self.desired.brightness
            if patch.brightness is None
            else patch.brightness,
            color_temp_kelvin=self.desired.color_temp_kelvin
            if patch.color_temp_kelvin is None
            else patch.color_temp_kelvin,
        )


@pytest.fixture
def light():
    from custom_components.zigbee_led_orchestrator.light import OrchestratorLight

    runtime = RuntimeStub()
    return OrchestratorLight(
        runtime, "entry-123", "light_a", 2000, 6500, supports_color_temp=True
    ), runtime


async def test_toggle_uses_desired_during_boot(light):
    entity, runtime = light
    await entity.async_turn_on()
    assert entity.is_on
    await entity.async_toggle()
    assert runtime.patches[-1].on is False
    assert not entity.is_on


async def test_turn_on_passes_kelvin_and_brightness(light):
    entity, runtime = light
    await entity.async_turn_on(brightness=123, color_temp_kelvin=3500)
    assert runtime.patches[-1].brightness == 123
    assert runtime.patches[-1].color_temp_kelvin == 3500


async def test_zero_brightness_maps_to_off(light):
    entity, runtime = light
    await entity.async_turn_on(brightness=0)
    assert runtime.patches[-1].on is False


async def test_unavailable_control_is_rejected(light):
    entity, runtime = light
    runtime.available = False
    assert entity.available is False
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()


def test_entity_exposes_capability_limits(light):
    entity, _ = light
    assert entity.min_color_temp_kelvin == 2000
    assert entity.max_color_temp_kelvin == 6500


def test_rename_keeps_unique_id(light):
    entity, _ = light
    assert entity.name == "Light A"
    assert entity.unique_id == "entry-123_light_a"


def test_configured_name_is_the_virtual_entity_name():
    from custom_components.zigbee_led_orchestrator.light import OrchestratorLight
    from custom_components.zigbee_led_orchestrator.sensor import PhaseSensor

    runtime = RuntimeStub()
    runtime.config = type(
        "Config",
        (),
        {"lights": [type("Light", (), {"id": "light_1", "name": "Kuchnia"})()]},
    )()
    entity = OrchestratorLight(
        runtime, "entry-123", "light_1", 2000, 6500, name="Kuchnia"
    )
    sensor = PhaseSensor(runtime, "entry-123", "light_1")
    assert entity.name == "Kuchnia"
    assert entity.unique_id == "entry-123_light_1"
    assert sensor.name == "Kuchnia phase"
    assert sensor.unique_id == "entry-123_light_1_phase"


async def test_turn_on_clamps_color_temperature_to_limits(light):
    entity, runtime = light
    await entity.async_turn_on(color_temp_kelvin=7000)
    assert runtime.patches[-1].color_temp_kelvin == 6500


async def test_diagnostics_remain_known_when_control_unavailable(light):
    entity, runtime = light
    runtime.available = False
    assert entity.extra_state_attributes == {
        "phase": "POWERING",
        "effective_state": "UNKNOWN",
        "confirmed": False,
    }


async def test_entity_subscribes_and_unsubscribes_to_runtime(light, monkeypatch):
    entity, runtime = light
    removed = []
    callbacks = []
    writes = []
    monkeypatch.setattr(
        runtime,
        "subscribe",
        lambda cb: callbacks.append(cb) or (lambda: removed.append(True)),
        raising=False,
    )
    monkeypatch.setattr(entity, "async_on_remove", lambda cb: removed.append(cb))
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: writes.append(True))
    await entity.async_added_to_hass()
    callbacks[0](frozenset({"unrelated"}))
    assert not writes
    callbacks[0](frozenset({"light_a"}))
    assert writes == [True]
    removed[0]()
    assert removed[-1] is True


async def test_unknown_color_capability_is_not_advertised_or_sent():
    from custom_components.zigbee_led_orchestrator.light import OrchestratorLight

    runtime = RuntimeStub()
    entity = OrchestratorLight(
        runtime, "entry", "light_a", 2000, 6500, supports_color_temp=False
    )
    assert entity.supported_color_modes != {"color_temp"}
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on(color_temp_kelvin=3500)
    assert not runtime.patches


async def test_platform_creates_entities_for_configured_lights_and_group():
    from types import SimpleNamespace

    from custom_components.zigbee_led_orchestrator.configuration import validate_config
    from custom_components.zigbee_led_orchestrator.light import async_setup_entry
    from tests.unit.test_configuration import valid_config

    runtime = RuntimeStub()
    runtime.config = validate_config(valid_config())
    entry = SimpleNamespace(entry_id="entry-123", runtime_data=runtime)
    entities = []
    hass = SimpleNamespace(config=SimpleNamespace(language="en"))
    await async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 3
    assert {entity.unique_id for entity in entities} == {
        "entry-123_light_a",
        "entry-123_light_b",
        "entry-123_group_a",
    }
    assert {entity.device_info["name"] for entity in entities} >= {
        "Light A",
        "Light B",
    }
    assert {entity.device_info["model"] for entity in entities} == {
        "LED circuit",
        "Circuit group",
    }
    assert all(entity.name is None for entity in entities)


async def test_capabilities_follow_verified_runtime_metadata(light, monkeypatch):
    from custom_components.zigbee_led_orchestrator.light_capabilities import (
        LightCapabilities,
    )

    entity, runtime = light
    callbacks = []
    monkeypatch.setattr(
        runtime,
        "subscribe",
        lambda cb: callbacks.append(cb) or (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        runtime,
        "light_capabilities",
        lambda light_id: LightCapabilities(None, None),
        raising=False,
    )
    monkeypatch.setattr(entity, "async_on_remove", lambda cb: None)
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    callbacks[0](frozenset({"light_a"}))
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on(color_temp_kelvin=3000)
