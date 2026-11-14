"""Persistent preferences hold validated brightness/CCT only, never power."""

import asyncio

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.models import ControlMode, IntentPatch
from tests.ha.test_runtime import FakeAdapter
from tests.unit.test_configuration import valid_config


@pytest.fixture
async def runtime():
    from custom_components.zigbee_led_orchestrator.runtime import Runtime

    adapter = FakeAdapter()
    instance = Runtime(validate_config(valid_config()), adapter)
    await instance.start()
    yield instance, adapter
    await instance.close()


def parse(raw):
    from custom_components.zigbee_led_orchestrator.preferences import (
        load_preferences,
    )

    return load_preferences(raw, validate_config(valid_config()))


async def test_restore_never_restores_power(runtime):
    from custom_components.zigbee_led_orchestrator.preferences import Preference

    instance, adapter = runtime
    instance.restore_preferences({"light_a": Preference(100, 3000)})
    snapshot = instance.snapshot("light_a")
    assert (snapshot.desired.brightness, snapshot.desired.color_temp_kelvin) == (
        100,
        3000,
    )
    assert snapshot.desired.on is False
    instance.set_mode(ControlMode.ACTIVE)
    await asyncio.sleep(0.2)
    assert not adapter.sent


async def test_restore_is_only_allowed_before_control(runtime):
    from custom_components.zigbee_led_orchestrator.preferences import Preference

    instance, _ = runtime
    instance.set_mode(ControlMode.ACTIVE)
    with pytest.raises(RuntimeError):
        instance.restore_preferences({"light_a": Preference(100, None)})


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"version": 2, "lights": {}},
        {"version": 1},
        {"version": 1, "lights": []},
        {"version": 1, "lights": {"unknown": {"brightness": 5}}},
        {"version": 1, "lights": {"light_a": {"brightness": True}}},
        {"version": 1, "lights": {"light_a": {"brightness": 0}}},
        {"version": 1, "lights": {"light_a": {"brightness": 256}}},
        {"version": 1, "lights": {"light_a": {"color_temp_kelvin": 100}}},
        {"version": 1, "lights": {"light_a": {"color_temp_kelvin": 3000.5}}},
        {"version": 1, "lights": {"light_a": {"on": True}}},
        {"version": 1, "lights": {"light_a": "bright"}},
    ],
)
def test_corrupt_preferences_fall_back_without_publish(raw):
    preferences, error = parse(raw)
    assert preferences == {}
    assert error


def test_valid_preferences_load_without_error():
    preferences, error = parse(
        {
            "version": 1,
            "lights": {"light_a": {"brightness": 10, "color_temp_kelvin": 3000}},
        }
    )
    assert error is None
    assert preferences["light_a"].brightness == 10


def test_missing_storage_is_not_an_error():
    assert parse(None) == ({}, None)


def test_serialization_has_no_power_or_commands():
    from custom_components.zigbee_led_orchestrator.preferences import (
        Preference,
        serialize_preferences,
    )

    data = serialize_preferences({"light_a": Preference(10, None)})
    assert data == {"version": 1, "lights": {"light_a": {"brightness": 10}}}


def enable_brightness(adapter):
    topology = adapter.topology
    entries = []
    for device in topology.devices():
        exposes = device.capabilities
        if device.topic.endswith(("led1", "led2")):
            exposes[0]["features"] += [
                {
                    "name": "brightness",
                    "property": "brightness",
                    "access": 7,
                    "value_min": 1,
                    "value_max": 254,
                },
                {
                    "name": "color_temp",
                    "property": "color_temp",
                    "access": 7,
                    "value_min": 153,
                    "value_max": 500,
                },
            ]
        entries.append(
            {
                "ieee_address": device.ieee,
                "friendly_name": device.topic.split("/", 1)[1],
                "endpoints": {
                    str(endpoint): (
                        {"name": device.endpoint_name(endpoint)}
                        if device.endpoint_name(endpoint)
                        else {}
                    )
                    for endpoint in device.endpoints
                },
                "definition": {"exposes": exposes},
            }
        )
    groups = [
        {
            "id": group_id,
            "friendly_name": group.topic.split("/", 1)[1],
            "members": [
                {"ieee_address": ieee, "endpoint": endpoint}
                for ieee, endpoint in group.members
            ],
        }
        for group_id, group in topology.groups()
    ]
    topology.update_devices(entries)
    topology.update_groups(groups)


async def test_runtime_reports_parameter_changes_for_saving_but_not_power_only(
    runtime,
):
    instance, adapter = runtime
    enable_brightness(adapter)
    saved = []
    instance.set_preferences_listener(lambda: saved.append(instance.preferences()))
    instance.set_mode(ControlMode.ACTIVE)
    instance.submit(("light_a",), IntentPatch(on=False), source="ui")
    assert not saved
    instance.submit(("light_a",), IntentPatch(brightness=50), source="ui")
    assert len(saved) == 1
    assert saved[0]["light_a"].brightness == 50
    assert saved[0]["light_b"].brightness is None
    instance.submit_group("group_a", IntentPatch(color_temp_kelvin=3000), source="ui")
    assert len(saved) == 2
    assert saved[1]["light_b"].color_temp_kelvin == 3000
    await asyncio.sleep(0)


async def test_store_round_trip_persists_no_power(hass, hass_storage):
    from custom_components.zigbee_led_orchestrator.preferences import (
        Preference,
        PreferencesStore,
    )

    store = PreferencesStore(hass, "entry-1")
    await store.async_save({"light_a": Preference(10, 3000)})
    stored = hass_storage["zigbee_led_orchestrator.entry-1"]
    assert stored["version"] == 1
    assert stored["data"] == {
        "version": 1,
        "lights": {"light_a": {"brightness": 10, "color_temp_kelvin": 3000}},
    }
    assert await store.async_load() == stored["data"]
