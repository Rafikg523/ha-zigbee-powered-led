"""Pilot ON/OFF via raw report protocol and real runtime; no trusted state model."""

from custom_components.zigbee_led_orchestrator.models import ControlMode
from tests.scenarios.harness import RELAY
from tests.unit.test_configuration import valid_config


async def test_raw_reports_drive_on_and_physical_off_without_replay(rig):
    config = valid_config()
    config["report_extension"] = True
    await rig.setup(config)
    rig.adapters[-1].trust_reports = False
    await rig.publish_catalog()
    handshake = rig.publications()[0]
    assert handshake[0] == "zigbee2mqtt/orchestrator/subscribe"
    session = handshake[1]["session"]
    sequence = 0

    async def raw(ieee, endpoint, cluster, data):
        nonlocal sequence
        sequence += 1
        rig.fire(
            "zigbee2mqtt/orchestrator/report",
            {
                "session": session,
                "sequence": sequence,
                "type": "attributeReport",
                "ieee": ieee,
                "endpoint": endpoint,
                "cluster": cluster,
                "data": data,
            },
        )
        await rig.settle()
        await rig.advance(0.01)

    rig.fire(
        "zigbee2mqtt/orchestrator/report",
        {"session": session, "sequence": 0, "type": "ready"},
    )
    await rig.settle()
    relay = "0x0000000000000001"
    led = "0x0000000000000002"
    await raw(relay, 1, "genOnOff", {"onOff": 0})
    await raw(relay, 2, "genOnOff", {"onOff": 0})
    await rig.advance(1)
    await rig.enable_active()
    assert rig.runtime.mode == ControlMode.ACTIVE
    await rig.light("turn_on", "light_a", brightness=180, color_temp_kelvin=3000)
    await rig.advance(0.01)
    assert rig.publications()[-1] == (f"{RELAY}/set", {"state_l1": "ON"})
    await raw(relay, 1, "genOnOff", {"onOff": 1})
    # A genuine partial brightness report suffices for readiness, not confirmation.
    await raw(led, 1, "genLevelCtrl", {"currentLevel": 50})
    await rig.advance(3)
    assert rig.publications()[-2][0] == "zigbee2mqtt/room/led1/set"
    assert rig.publications()[-1] == (
        "zigbee2mqtt/room/led1/get",
        {"state": "", "brightness": "", "color_temp": ""},
    )
    assert not rig.runtime.snapshot("light_a").confirmed
    await raw(led, 1, "genOnOff", {"onOff": 1})
    await raw(led, 1, "genLevelCtrl", {"currentLevel": 179})
    await raw(led, 1, "lightingColorCtrl", {"colorTemperature": 333})
    assert rig.runtime.snapshot("light_a").confirmed
    await raw(relay, 1, "genOnOff", {"onOff": 0})
    assert not rig.runtime.snapshot("light_a").desired.on
    count = len(rig.publications())
    await rig.advance(30)
    assert len(rig.publications()) == count
