"""Loaded entry must stay ACTIVE after a routine Zigbee2MQTT catalog refresh.

Z2M republishes bridge/devices on announce, interview and network-address
changes. That refresh is not a new mapping and must not replay an old ON.
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zigbee_led_orchestrator.models import ControlMode
from tests.unit.test_configuration import valid_config


async def command_is_inert_light(rig, suffix: str) -> None:
    published = list(rig.publications())
    desired = rig.runtime.snapshot(suffix).desired
    try:
        await rig.light("turn_on", suffix, brightness=180)
    except HomeAssistantError:
        pass
    await rig.advance(1)
    assert rig.publications() == published
    assert rig.runtime.snapshot(suffix).desired == desired


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


async def test_unready_report_extension_does_not_stick_after_catalog_refresh(rig):
    config = valid_config()
    config["report_extension"] = True
    await rig.setup(config)
    await rig.publish_catalog()
    with pytest.raises(RuntimeError, match="report extension unavailable"):
        rig.runtime.set_mode(ControlMode.ACTIVE)
    assert rig.runtime.mode == ControlMode.OBSERVE

    rig.fire("zigbee2mqtt/bridge/devices", rig.pilot["devices"], retain=False)
    await rig.settle()
    assert rig.runtime.mode == ControlMode.OBSERVE
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
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.runtime.diagnostics()["topology_stale"] is False
    assert rig.publications() == published


async def test_one_missing_device_disables_only_that_circuit(rig):
    await rig.setup()
    await rig.boot_to_off()
    devices = [
        device
        for device in rig.pilot["devices"]
        if device["ieee_address"] != "0x0000000000000002"
    ]
    rig.fire("zigbee2mqtt/bridge/devices", devices, retain=False)
    await rig.settle()
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert not rig.runtime.snapshot("light_a").available
    assert rig.runtime.snapshot("light_b").available
    assert any(key.startswith("light_") for key in rig.runtime.notices)
    published = list(rig.publications())
    await rig.light("turn_on", "light_b", brightness=180)
    await rig.advance(0.01)
    assert len(rig.publications()) == len(published) + 1
    await command_is_inert_light(rig, "light_a")


async def test_every_missing_device_stays_inactive(rig):
    await rig.setup()
    await rig.publish_catalog()
    rig.fire("zigbee2mqtt/bridge/devices", [], retain=False)
    await rig.settle()
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert "catalog" in rig.runtime.notices
    with pytest.raises(RuntimeError, match="configured devices missing"):
        rig.runtime.set_mode(ControlMode.ACTIVE)
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.publications() == []


async def test_bridge_offline_is_not_a_topology_error(rig):
    await rig.setup()
    rig.fire("zigbee2mqtt/bridge/devices", rig.pilot["devices"], retain=True)
    await rig.settle()
    rig.fire("zigbee2mqtt/bridge/groups", rig.pilot["groups"], retain=True)
    await rig.settle()
    with pytest.raises(RuntimeError, match="bridge offline"):
        rig.runtime.set_mode(ControlMode.ACTIVE)
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.publications() == []
