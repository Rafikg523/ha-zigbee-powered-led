"""Synthetic Zigbee2MQTT topology metadata tests."""

import pytest

from custom_components.zigbee_led_orchestrator.topology import Topology


def device(ieee="0x00124b0000000001", name="room/light"):
    return {"ieee_address": ieee, "friendly_name": name, "definition": {"exposes": []}}


def test_nested_friendly_name_preserved():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    assert (
        topology.resolve_device("0x00124b0000000001").topic == "zigbee2mqtt/room/light"
    )


def test_missing_endpoint_metadata_does_not_invent_endpoint_one():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    with pytest.raises(KeyError):
        topology.resolve_device("0x00124b0000000001").endpoint_name(1)


def test_reused_name_or_bridge_collision_is_rejected():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    with pytest.raises(ValueError):
        topology.update_devices([device(), device("0x00124b0000000002")])
    with pytest.raises(KeyError):
        topology.resolve_device("0x00124b0000000001")
    for name in ("bridge", "bridge/state", "room/+", "room/#"):
        with pytest.raises(ValueError):
            topology.update_devices([device(name=name)])


def test_group_members_are_checked_against_device_endpoints():
    topology = Topology("zigbee2mqtt")
    entry = device()
    entry["endpoints"] = {"1": {}}
    topology.update_devices([entry])
    topology.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "room/group",
                "members": [{"ieee_address": "0x00124b0000000001", "endpoint": 1}],
            }
        ]
    )
    assert topology.resolve_group(1).members == frozenset({("0x00124b0000000001", 1)})
    with pytest.raises(ValueError):
        topology.update_groups(
            [
                {
                    "id": 1,
                    "friendly_name": "room/group",
                    "members": [{"ieee_address": "0x00124b0000000001", "endpoint": 2}],
                }
            ]
        )


def test_invalid_group_snapshot_revokes_old_group():
    topology = Topology("zigbee2mqtt")
    entry = device()
    entry["endpoints"] = {"1": {}}
    topology.update_devices([entry])
    topology.update_groups([{"id": 1, "friendly_name": "group", "members": []}])
    with pytest.raises(ValueError):
        topology.update_groups(
            [
                {
                    "id": 1,
                    "friendly_name": "group",
                    "members": [{"ieee_address": entry["ieee_address"], "endpoint": 2}],
                }
            ]
        )
    with pytest.raises(KeyError):
        topology.resolve_group(1)


def test_invalid_device_snapshot_revokes_old_address():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    with pytest.raises(ValueError):
        topology.update_devices([device(), device("0x00124b0000000002")])
    with pytest.raises(KeyError):
        topology.resolve_device("0x00124b0000000001")


def test_name_reused_by_other_ieee_is_rejected():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    with pytest.raises(ValueError):
        topology.update_devices([device("0x00124b0000000002")])
    with pytest.raises(KeyError):
        topology.resolve_device("0x00124b0000000001")


def test_device_capabilities_are_available_without_mutable_catalog_state():
    topology = Topology("zigbee2mqtt")
    entry = device()
    entry["definition"] = {"exposes": [{"type": "light"}]}
    topology.update_devices([entry])
    capabilities = topology.resolve_device(entry["ieee_address"]).capabilities
    assert capabilities == [{"type": "light"}]
    capabilities.clear()
    assert topology.resolve_device(entry["ieee_address"]).capabilities == [
        {"type": "light"}
    ]


def test_group_name_reuse_for_another_group_is_rejected():
    topology = Topology("zigbee2mqtt")
    topology.update_devices([device()])
    topology.update_groups([{"id": 1, "friendly_name": "group", "members": []}])
    with pytest.raises(ValueError):
        topology.update_groups([{"id": 2, "friendly_name": "group", "members": []}])
    with pytest.raises(KeyError):
        topology.resolve_group(1)


def test_device_refresh_keeps_only_groups_that_still_resolve():
    topology = Topology("zigbee2mqtt")
    entry = device()
    entry["endpoints"] = {"1": {}}
    topology.update_devices([entry])
    topology.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group",
                "members": [{"ieee_address": entry["ieee_address"], "endpoint": 1}],
            }
        ]
    )
    topology.update_devices([entry])
    assert topology.groups_seen
    assert topology.resolve_group(1).members == frozenset({(entry["ieee_address"], 1)})
    entry["endpoints"] = {"2": {}}
    topology.update_devices([entry])
    with pytest.raises(KeyError):
        topology.resolve_group(1)
    with pytest.raises(ValueError):
        topology.update_devices([device(), device("0x00124b0000000002")])
    with pytest.raises(KeyError):
        topology.resolve_device(entry["ieee_address"])


def test_endpoint_name_is_retained_from_device_catalog():
    entry = device()
    entry["endpoints"] = {"1": {"name": "left"}, "2": {"name": "right"}}
    topology = Topology("zigbee2mqtt")
    topology.update_devices([entry])
    resolved = topology.resolve_device(entry["ieee_address"])
    assert resolved.endpoint_name(2) == "right"
    with pytest.raises(KeyError):
        resolved.endpoint_name(3)
