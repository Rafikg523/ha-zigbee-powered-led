"""Loaded entry must enter ACTIVE after a routine Zigbee2MQTT catalog refresh.

Z2M republishes bridge/devices on announce, interview and network-address
changes. That refresh is not a new mapping. It must not sticky-fail the next
explicit ACTIVE, and it must not replay an old ON.
"""

from custom_components.zigbee_led_orchestrator.models import ControlMode
from tests.unit.test_configuration import valid_config


async def test_loaded_entry_stays_active_when_devices_are_republished(rig):
    await rig.setup()
    await rig.boot_to_off()
    assert rig.runtime.mode == ControlMode.ACTIVE
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    published = list(rig.publications())
    assert published
    assert rig.runtime.snapshot("light_a").desired.on is True

    # An already-subscribed client receives a retained republish with retain cleared.
    rig.fire("zigbee2mqtt/bridge/devices", rig.pilot["devices"], retain=False)
    await rig.settle()
    await rig.advance(1)

    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.runtime.diagnostics()["topology_stale"] is False
    assert rig.adapters[-1].topology.resolve_group(1).members
    assert rig.runtime.snapshot("light_a").desired.on is True
    assert rig.publications() == published
    result = await rig.enable_active()
    assert result["type"] == "create_entry"
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.publications() == published


async def test_unready_report_extension_does_not_stick_after_catalog_refresh(rig):
    config = valid_config()
    config["report_extension"] = True
    await rig.setup(config)
    await rig.publish_catalog()
    refused = await rig.enable_active()
    assert refused["type"] == "form"
    assert refused["errors"]["base"] == "report_extension_unavailable"
    assert rig.runtime.mode == ControlMode.OBSERVE

    rig.fire("zigbee2mqtt/bridge/devices", rig.pilot["devices"], retain=False)
    await rig.settle()
    handshake = next(
        payload
        for topic, payload in reversed(rig.publications())
        if topic.endswith("/orchestrator/subscribe")
    )
    rig.fire(
        "zigbee2mqtt/orchestrator/report",
        {"session": handshake["session"], "sequence": 0, "type": "ready"},
    )
    await rig.settle()
    published = list(rig.publications())
    enabled = await rig.enable_active()
    assert enabled["type"] == "create_entry"
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.runtime.diagnostics()["topology_stale"] is False
    assert rig.publications() == published


async def test_missing_mapped_device_names_the_mismatch(rig):
    await rig.setup()
    await rig.publish_catalog()
    devices = [
        device
        for device in rig.pilot["devices"]
        if device["ieee_address"] != "0x0000000000000002"
    ]
    rig.fire("zigbee2mqtt/bridge/devices", devices, retain=False)
    await rig.settle()
    result = await rig.enable_active()
    assert result["type"] == "form"
    assert result["errors"]["base"] == "mapping_mismatch"
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.publications() == []


async def test_bridge_offline_is_not_a_topology_error(rig):
    await rig.setup()
    rig.fire("zigbee2mqtt/bridge/devices", rig.pilot["devices"], retain=True)
    await rig.settle()
    rig.fire("zigbee2mqtt/bridge/groups", rig.pilot["groups"], retain=True)
    await rig.settle()
    result = await rig.enable_active()
    assert result["type"] == "form"
    assert result["errors"]["base"] == "bridge_unavailable"
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.publications() == []
