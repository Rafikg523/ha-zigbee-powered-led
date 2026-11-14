"""Production adapter consumes the optional raw report bridge, no trust override."""

import json

import pytest
from homeassistant.components import mqtt

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.models import EvidenceQuality
from custom_components.zigbee_led_orchestrator.mqtt_adapter import MqttAdapter
from tests.ha.test_mqtt_adapter import devices, message

pytest_plugins = ["tests.ha.test_mqtt_adapter"]
from tests.unit.test_configuration import valid_config


@pytest.fixture
async def bridge(hass, mqtt_api):
    data = valid_config()
    data["report_extension"] = True
    observations = []
    adapter = MqttAdapter(hass, validate_config(data))
    await adapter.start(observations.append)
    callbacks, _, _ = mqtt_api
    await message(callbacks, "zigbee2mqtt/bridge/devices", devices())
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    yield adapter, observations
    await adapter.close()


def handshake_request():
    args = mqtt.async_publish.await_args
    assert args.args[1] == "zigbee2mqtt/orchestrator/subscribe"
    assert args.kwargs["retain"] is False
    return json.loads(args.args[2])


async def test_opt_in_metadata_handshake_and_raw_report(bridge, mqtt_api):
    adapter, observations = bridge
    callbacks, _, _ = mqtt_api
    request = handshake_request()
    assert len(request["targets"]) == 4
    with pytest.raises(RuntimeError):
        adapter.set_active(True)
    await message(
        callbacks,
        "zigbee2mqtt/orchestrator/report",
        {
            "session": request["session"],
            "sequence": 0,
            "type": "ready",
        },
    )
    adapter.set_active(True)
    await message(
        callbacks,
        "zigbee2mqtt/orchestrator/report",
        {
            "session": request["session"],
            "sequence": 1,
            "type": "attributeReport",
            "ieee": "0x0000000000000001",
            "endpoint": 1,
            "cluster": "genOnOff",
            "data": {"onOff": 1},
        },
    )
    assert observations[-1].quality == EvidenceQuality.DEVICE_REPORT
    count = len(observations)
    # The ordinary merged state must not erase the raw device proof.
    await message(callbacks, "zigbee2mqtt/room/relay", {"state_l1": "OFF"})
    assert len(observations) == count


async def test_retained_or_previous_session_cannot_supply_evidence(bridge, mqtt_api):
    adapter, observations = bridge
    callbacks, _, status = mqtt_api
    old = handshake_request()["session"]
    report = {
        "session": old,
        "sequence": 1,
        "type": "attributeReport",
        "ieee": "0x0000000000000001",
        "endpoint": 1,
        "cluster": "genOnOff",
        "data": {"onOff": 1},
    }
    count = len(observations)
    await message(callbacks, "zigbee2mqtt/orchestrator/report", report, retain=True)
    assert len(observations) == count
    status[0](False)
    status[0](True)
    await adapter._hass.async_block_till_done()
    await message(callbacks, "zigbee2mqtt/bridge/state", {"state": "online"})
    assert handshake_request()["session"] != old
    count = len(observations)
    await message(callbacks, "zigbee2mqtt/orchestrator/report", report)
    assert len(observations) == count


async def test_publish_completion_discards_cached_and_inflight_parameter_proof(
    bridge, mqtt_api, monkeypatch
):
    from unittest.mock import AsyncMock

    adapter, observations = bridge
    callbacks, _, _ = mqtt_api
    session = handshake_request()["session"]

    async def raw(sequence, ieee="0x0000000000000001", cluster="genOnOff", data=None):
        await message(
            callbacks,
            "zigbee2mqtt/orchestrator/report",
            {
                "session": session,
                "sequence": sequence,
                "type": "attributeReport",
                "ieee": ieee,
                "endpoint": 1,
                "cluster": cluster,
                "data": {"onOff": 1} if data is None else data,
            },
        )

    await message(
        callbacks,
        "zigbee2mqtt/orchestrator/report",
        {"session": session, "sequence": 0, "type": "ready"},
    )
    await raw(1)
    led = "0x0000000000000002"
    await raw(2, led)
    await raw(3, led, "genLevelCtrl", {"currentLevel": 179})
    adapter.set_active(True)

    async def during_publish(*args, **kwargs):
        await raw(4, led)

    monkeypatch.setattr(mqtt, "async_publish", AsyncMock(side_effect=during_publish))
    await adapter.publish(
        "zigbee2mqtt/room/led/set", {"state": "ON", "color_temp": 333}
    )
    await raw(5, led, "lightingColorCtrl", {"colorTemperature": 333})
    assert observations[-1].brightness is None
    assert observations[-1].state.value == "UNKNOWN"


async def test_gap_revokes_adapter_authority_and_runtime_evidence(bridge, mqtt_api):
    adapter, observations = bridge
    callbacks, _, _ = mqtt_api
    session = handshake_request()["session"]
    await message(
        callbacks,
        "zigbee2mqtt/orchestrator/report",
        {"session": session, "sequence": 0, "type": "ready"},
    )
    adapter.set_active(True)
    await message(
        callbacks,
        "zigbee2mqtt/orchestrator/report",
        {
            "session": session,
            "sequence": 2,
            "type": "attributeReport",
            "ieee": "0x0000000000000001",
            "endpoint": 1,
            "cluster": "genOnOff",
            "data": {"onOff": 1},
        },
    )
    assert observations[-1].online is False
    assert adapter.last_error == "report_sequence_gap; resync required"
    with pytest.raises(RuntimeError):
        await adapter.publish("zigbee2mqtt/room/relay/set", {"state_l1": "ON"})
