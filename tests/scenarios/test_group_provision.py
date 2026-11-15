"""The integration creates its own Z2M group and does not adopt remembered state."""

from custom_components.zigbee_led_orchestrator.models import ControlMode
from tests.scenarios.harness import LED, RELAY
from tests.unit.test_configuration import valid_config

FOREIGN = {
    "id": 1,
    "friendly_name": "room/group",
    "members": [
        {"ieee_address": "0x0000000000000002", "endpoint": 1},
        {"ieee_address": "0x0000000000000003", "endpoint": 1},
    ],
}


def named_config():
    config = valid_config()
    config["groups"][0]["name"] = "Salon"
    config["groups"][0]["z2m_id"] = 2
    return config


def orchestrator(members):
    return {
        "id": 2,
        "friendly_name": "Salon_orchestrator",
        "members": members,
    }


async def test_named_group_is_created_and_group_state_does_not_change_intent(rig):
    await rig.setup(named_config())
    assert rig.state("group_a").name == "Salon_orchestrator"
    rig.pilot["groups"] = [FOREIGN]
    await rig.publish_catalog()
    created = [
        item for item in rig.admin_publications() if item[0].endswith("/group/add")
    ]
    assert created[-1][1]["friendly_name"] == "Salon_orchestrator"
    assert created[-1][1]["id"] == 2
    assert all(
        "room/group" not in item[1].values() for item in rig.admin_publications()
    )
    rig.fire(
        "zigbee2mqtt/bridge/response/group/add",
        {
            "status": "ok",
            "transaction": created[-1][1]["transaction"],
            "data": {"id": 2, "friendly_name": "Salon_orchestrator"},
        },
    )
    await rig.settle()
    options = [
        item for item in rig.admin_publications() if item[0].endswith("/group/options")
    ]
    assert options[-1][1]["options"] == {"optimistic": False, "retain": False}
    rig.fire(
        "zigbee2mqtt/bridge/response/group/options",
        {
            "status": "ok",
            "transaction": options[-1][1]["transaction"],
            "data": {
                "to": {"optimistic": False, "retain": False},
                "restart_required": False,
            },
        },
    )
    await rig.settle()
    await rig.report_relay("ON", "ON")
    # A broker clears the retain flag when it forwards a new publish to an
    # existing subscription. HA also drops a second retained copy of one topic.
    rig.fire("zigbee2mqtt/bridge/groups", [FOREIGN, orchestrator([])])
    await rig.settle()
    for device, endpoint in (("room/led1", 1), ("room/led2", 1)):
        added = [
            item
            for item in rig.admin_publications()
            if item[0].endswith("/members/add") and item[1]["device"] == device
        ]
        assert added[-1][1]["group"] == "Salon_orchestrator"
        assert added[-1][1]["endpoint"] == endpoint
        rig.fire(
            "zigbee2mqtt/bridge/response/group/members/add",
            {
                "status": "ok",
                "transaction": added[-1][1]["transaction"],
                "data": {"group": "Salon_orchestrator", "device": device},
            },
        )
        members = [
            {"ieee_address": "0x0000000000000002", "endpoint": 1},
        ]
        if device == "room/led2":
            members.append({"ieee_address": "0x0000000000000003", "endpoint": 1})
        rig.fire("zigbee2mqtt/bridge/groups", [FOREIGN, orchestrator(members)])
        await rig.settle()
    assert rig.entry.data["groups"][0]["z2m_id"] == 2
    assert not any(
        item[0].endswith("/members/remove_all") for item in rig.admin_publications()
    )
    await rig.advance(0.01)
    await rig.report_relay("OFF", "OFF")
    await rig.advance(0.5)
    assert rig.runtime.mode == ControlMode.ACTIVE

    before = len(rig.publications())
    await rig.light("turn_on", "group_a", brightness=200)
    await rig.advance(0.5)
    await rig.report_relay("ON", "ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.report_led("light_b", "ON")
    await rig.advance(3.5)
    group_sets = [
        item
        for item in rig.publications()[before:]
        if item[0] == "zigbee2mqtt/Salon_orchestrator/set"
    ]
    assert len(group_sets) == 1
    assert f"{LED['light_a']}/set" not in [topic for topic, _ in rig.publications()]
    generation = {
        light: rig.runtime.snapshot(light).desired.generation for light in LED
    }
    rig.fire(
        "zigbee2mqtt/Salon_orchestrator",
        {"state": "OFF", "brightness": 1},
        retain=True,
    )
    rig.fire(LED["light_a"], {"state": "OFF", "brightness": 1})
    await rig.settle()
    for light, previous in generation.items():
        assert rig.runtime.snapshot(light).desired.generation == previous
        assert rig.runtime.snapshot(light).desired.on
        assert rig.runtime.snapshot(light).desired.brightness == 200


async def test_incomplete_membership_controls_members_individually(rig):
    await rig.setup(named_config())
    rig.pilot["groups"] = [
        orchestrator([{"ieee_address": "0x0000000000000002", "endpoint": 1}])
    ]
    await rig.publish_catalog()
    options = [
        item for item in rig.admin_publications() if item[0].endswith("/group/options")
    ]
    rig.fire(
        "zigbee2mqtt/bridge/response/group/options",
        {
            "status": "ok",
            "transaction": options[-1][1]["transaction"],
            "data": {"to": {"optimistic": False, "retain": False}},
        },
    )
    await rig.settle()
    await rig.report_relay("OFF", "OFF")
    await rig.advance(0.5)
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.state("group_a").state == "unavailable"
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.light("turn_on", "light_b", brightness=180)
    await rig.advance(0.5)
    await rig.report_relay("ON", "ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.report_led("light_b", "ON")
    await rig.advance(3.5)
    topics = [topic for topic, _ in rig.publications()]
    assert f"{LED['light_a']}/set" in topics
    assert f"{LED['light_b']}/set" in topics
    assert "zigbee2mqtt/Salon_orchestrator/set" not in topics
    assert f"{RELAY}/set" in topics
