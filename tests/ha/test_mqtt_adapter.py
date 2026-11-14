"""MQTT adapter tests with synthetic messages and Home Assistant MQTT APIs."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import mqtt

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.models import EvidenceQuality
from tests.unit.test_configuration import valid_config


@pytest.fixture
def mqtt_api(monkeypatch):
    callbacks = {}
    all_callbacks = []
    status = []

    async def subscribe(hass, topic, callback):
        callbacks[topic] = callback
        all_callbacks.append(callback)

        def remove():
            if callbacks.get(topic) is callback:
                del callbacks[topic]

        return remove

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", AsyncMock())
    monkeypatch.setattr(mqtt, "is_connected", lambda hass: True)
    monkeypatch.setattr(
        mqtt,
        "async_subscribe_connection_status",
        lambda hass, cb: status.append(cb) or (lambda: None),
    )
    return callbacks, all_callbacks, status


def devices(name="room/relay"):
    return [
        {
            "ieee_address": "0x0000000000000001",
            "friendly_name": name,
            "endpoints": {"1": {}, "2": {}},
            "definition": None,
        },
        {
            "ieee_address": "0x0000000000000002",
            "friendly_name": "room/led",
            "endpoints": {"1": {}},
            "definition": None,
        },
    ]


async def message(callbacks, topic, payload, retain=False):
    msg = SimpleNamespace(topic=topic, payload=json.dumps(payload), retain=retain)
    result = callbacks[topic](msg)
    if result is not None:
        await result


@pytest.fixture
async def adapter(hass, mqtt_api):
    from custom_components.zigbee_led_orchestrator.mqtt_adapter import MqttAdapter

    observations = []
    instance = MqttAdapter(hass, validate_config(valid_config()), now=lambda: 1.0)
    await instance.start(observations.append)
    yield instance, observations
    await instance.close()


async def test_publish_uses_ha_mqtt_without_retain(adapter, mqtt_api, hass):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "OFF"})
    mqtt.async_publish.assert_awaited_once_with(
        hass, "zigbee2mqtt/room/relay/set", '{"state_l1":"OFF"}', retain=False
    )
    with pytest.raises(ValueError):
        await instance.publish("zigbee2mqtt/room/relay/set", {}, retain=True)


async def test_malformed_payload_is_ignored_with_reason(adapter, mqtt_api):
    instance, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "unexpected"})
    assert not observations
    assert instance.last_error


async def test_rename_resubscribes_without_old_topic_send(adapter, mqtt_api):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices("room/renamed"))
    assert "zigbee2mqtt/room/relay" not in callbacks
    assert "zigbee2mqtt/room/renamed" in callbacks
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "ON"})


async def test_group_metadata_is_not_physical_event(adapter, mqtt_api):
    _instance, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices(), retain=True)
    await message(
        callbacks,
        "zigbee2mqtt/bridge/groups",
        [{"id": 1, "friendly_name": "group", "members": []}],
        retain=True,
    )
    assert not observations


async def test_optimistic_state_is_unconfirmed(adapter, mqtt_api):
    _, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/room/led", {"state": "ON"})
    assert observations[-1].quality == EvidenceQuality.UNKNOWN_SOURCE


async def test_callback_from_old_session_is_discarded_after_reconnect(
    adapter, mqtt_api
):
    _, observations = adapter
    callbacks, _all_callbacks, status = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    old = callbacks["zigbee2mqtt/room/led"]
    status[0](False)
    status[0](True)
    before = len(observations)
    result = old(
        SimpleNamespace(
            topic="zigbee2mqtt/room/led", payload='{"state":"ON"}', retain=False
        )
    )
    if result is not None:
        await result
    assert len(observations) == before


async def test_group_publish_requires_exact_configured_members(adapter, mqtt_api):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    all_devices = devices() + [
        {
            "ieee_address": "0x0000000000000003",
            "friendly_name": "room/led2",
            "endpoints": {"1": {}},
            "definition": None,
        }
    ]
    await message(callbacks, "zigbee2mqtt/bridge/devices", all_devices)
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    group = {
        "id": 1,
        "friendly_name": "room/group",
        "members": [
            {"ieee_address": "0x0000000000000002", "endpoint": 1},
            {"ieee_address": "0x0000000000000003", "endpoint": 1},
        ],
    }
    await message(callbacks, "zigbee2mqtt/bridge/groups", [group])
    await instance.publish("zigbee2mqtt/room/group/set", {"state": "OFF"})
    group["members"].pop()
    await message(callbacks, "zigbee2mqtt/bridge/groups", [group])
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/group/set", {"state": "ON"})


async def test_observe_mode_and_disconnect_forbid_publish(adapter, mqtt_api):
    instance, _ = adapter
    callbacks, _, status = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    instance.set_active(False)
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/led/set", {"state": "ON"})
    instance.set_active(True)
    status[0](False)
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/led/set", {"state": "ON"})


async def test_invalid_metadata_revokes_old_command_target(adapter, mqtt_api):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    await callbacks["zigbee2mqtt/bridge/devices"](
        SimpleNamespace(
            topic="zigbee2mqtt/bridge/devices", payload="{invalid", retain=True
        )
    )
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "ON"})


async def test_reconnect_requires_fresh_device_metadata(adapter, mqtt_api, hass):
    instance, _ = adapter
    callbacks, _, status = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    status[0](False)
    status[0](True)
    await hass.async_block_till_done()
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    instance.set_active(True)
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "ON"})


async def test_real_ha_mqtt_publish_boundary(
    hass, mqtt_mock, enable_custom_integrations
):
    from custom_components.zigbee_led_orchestrator.mqtt_adapter import MqttAdapter

    instance = MqttAdapter(hass, validate_config(valid_config()), now=lambda: 1.0)
    await instance.start(lambda observation: None)
    await instance._receive(
        SimpleNamespace(
            topic="zigbee2mqtt/bridge/devices",
            payload=json.dumps(devices()),
            retain=True,
        ),
        instance._session,
    )
    await instance._receive(
        SimpleNamespace(
            topic="zigbee2mqtt/bridge/state", payload='{"state":"online"}', retain=True
        ),
        instance._session,
    )
    instance.set_active(True)
    await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "OFF"})
    mqtt_mock.async_publish.assert_called_with(
        "zigbee2mqtt/room/relay/set",
        '{"state_l1":"OFF"}',
        0,
        False,
        message_expiry_interval=None,
    )
    await instance.close()
    await hass.config_entries.async_unload(
        hass.config_entries.async_entries("mqtt")[0].entry_id
    )
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def test_concurrent_renames_do_not_leave_stale_subscription(
    adapter, mqtt_api, monkeypatch
):
    import asyncio

    _instance, _ = adapter
    callbacks, _, _ = mqtt_api
    original = mqtt.async_subscribe
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(hass, topic, callback):
        if topic == "zigbee2mqtt/room/relay":
            started.set()
            await release.wait()
        return await original(hass, topic, callback)

    monkeypatch.setattr(mqtt, "async_subscribe", delayed)
    first = asyncio.create_task(
        message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    )
    await started.wait()
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices("room/new"))
    release.set()
    await first
    assert "zigbee2mqtt/room/relay" not in callbacks
    assert "zigbee2mqtt/room/new" in callbacks


async def test_bridge_restart_discards_old_callbacks_and_topology(
    adapter, mqtt_api, hass
):
    instance, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    old = callbacks["zigbee2mqtt/room/led"]
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "offline"})
    await hass.async_block_till_done()
    before = len(observations)
    await old(
        SimpleNamespace(
            topic="zigbee2mqtt/room/led", payload='{"state":"ON"}', retain=False
        )
    )
    assert len(observations) == before
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    instance.set_active(True)
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/led/set", {"state": "ON"})


async def test_cannot_arm_control_while_disconnected(adapter, mqtt_api):
    instance, _ = adapter
    _, _, status = mqtt_api
    status[0](False)
    with pytest.raises(RuntimeError):
        instance.set_active(True)


async def test_close_cancels_pending_reconnect_subscriptions(
    adapter, mqtt_api, hass, monkeypatch
):
    import asyncio

    instance, _ = adapter
    callbacks, _, status = mqtt_api
    original = mqtt.async_subscribe
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(hass, topic, callback):
        started.set()
        await release.wait()
        return await original(hass, topic, callback)

    monkeypatch.setattr(mqtt, "async_subscribe", delayed)
    status[0](False)
    status[0](True)
    await started.wait()
    await instance.close()
    assert not callbacks
    assert not instance._tasks
    release.set()
    await hass.async_block_till_done()
    assert not callbacks


async def test_initial_retained_group_before_devices_can_be_validated(
    adapter, mqtt_api
):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    group = {
        "id": 1,
        "friendly_name": "room/group",
        "members": [
            {"ieee_address": "0x0000000000000002", "endpoint": 1},
            {"ieee_address": "0x0000000000000003", "endpoint": 1},
        ],
    }
    await message(callbacks, "zigbee2mqtt/bridge/groups", [group], retain=True)
    await message(
        callbacks,
        "zigbee2mqtt/bridge/devices",
        devices()
        + [
            {
                "ieee_address": "0x0000000000000003",
                "friendly_name": "room/led2",
                "endpoints": {"1": {}},
                "definition": None,
            }
        ],
        retain=True,
    )
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    await instance.publish("zigbee2mqtt/room/group/set", {"state": "OFF"})


async def test_led_color_temperature_mireds_converted_to_kelvin(adapter, mqtt_api):
    _, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(
        callbacks,
        "zigbee2mqtt/room/led",
        {"state": "ON", "brightness": 180, "color_temp": 250},
    )
    assert observations[-1].brightness == 180
    assert observations[-1].color_temp_kelvin == 4000


async def test_device_leave_revokes_address_before_metadata_refresh(adapter, mqtt_api):
    instance, observations = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    before = len(observations)
    await message(
        callbacks,
        "zigbee2mqtt/bridge/event",
        {"type": "device_leave", "data": {"ieee_address": "0x0000000000000001"}},
    )
    assert len(observations) == before
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "ON"})


async def test_toggle_command_is_rejected(adapter, mqtt_api):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    with pytest.raises(ValueError):
        await instance.publish("zigbee2mqtt/room/relay/set", {"state_l1": "TOGGLE"})


async def test_topology_change_notifies_runtime_before_old_address_can_be_used(
    adapter, mqtt_api
):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    versions = []
    unsubscribe = instance.subscribe_topology(lambda version: versions.append(version))
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    assert versions == [instance.topology.version]
    unsubscribe()
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices("room/renamed"))
    assert len(versions) == 1


async def test_resync_revokes_control_and_rereads_catalog_without_publishing(
    adapter, mqtt_api, hass
):
    instance, _ = adapter
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
    old = callbacks["zigbee2mqtt/room/led"]
    mqtt.async_publish.reset_mock()

    await instance.resync()

    assert "zigbee2mqtt/room/led" not in callbacks
    assert {
        "zigbee2mqtt/bridge/state",
        "zigbee2mqtt/bridge/devices",
        "zigbee2mqtt/bridge/groups",
    } <= set(callbacks)
    with pytest.raises(RuntimeError):
        await instance.publish("zigbee2mqtt/room/led/set", {"state": "ON"})
    with pytest.raises(RuntimeError):
        instance.set_active(True)
    await old(
        SimpleNamespace(
            topic="zigbee2mqtt/room/led", payload='{"state":"ON"}', retain=False
        )
    )
    mqtt.async_publish.assert_not_called()
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    instance.set_active(True)
