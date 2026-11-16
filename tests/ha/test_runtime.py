"""Synthetic runtime lifecycle without a broker or physical devices."""

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.models import ControlMode, IntentPatch
from tests.unit.test_configuration import valid_config
from tests.unit.test_topology_config import topology


class FakeAdapter:
    def __init__(self):
        self.topology = topology()
        self.observation = None
        self.topology_callback = None
        self.active = False
        self.closed = False
        self.resynced = 0
        self.sent = []

    def subscribe_topology(self, callback):
        self.topology_callback = callback
        return lambda: setattr(self, "topology_callback", None)

    async def start(self, callback):
        self.observation = callback

    def set_active(self, active):
        self.active = active

    async def publish(self, topic, payload, *, retain=False):
        self.sent.append((topic, payload, retain))

    async def resync(self):
        self.resynced += 1

    async def close(self):
        self.closed = True


@pytest.fixture
async def runtime():
    from custom_components.zigbee_led_orchestrator.runtime import Runtime

    adapter = FakeAdapter()
    instance = Runtime(validate_config(valid_config()), adapter)
    await instance.start()
    yield instance, adapter
    await instance.close()


async def test_first_setup_is_observe_only(runtime):
    instance, adapter = runtime
    assert instance.mode == ControlMode.OBSERVE
    assert not adapter.active
    with pytest.raises(RuntimeError):
        instance.submit(("light_a",), IntentPatch(on=True), source="ui")
    assert not adapter.sent


async def test_circuit_delivery_identity_uses_ieee_shared_across_relay_channels(
    runtime,
):
    instance, _ = runtime
    first, second = (instance._circuit_for(light) for light in instance.config.lights)
    assert first.relay_device == second.relay_device == "zigbee2mqtt:0x0000000000000001"
    assert first.led_device == "zigbee2mqtt:0x0000000000000002"
    assert second.led_device == "zigbee2mqtt:0x0000000000000003"


async def test_group_membership_change_keeps_the_other_circuits(runtime):
    instance, adapter = runtime
    instance.set_mode(ControlMode.ACTIVE)
    assert adapter.active
    instance.submit(("light_a",), IntentPatch(on=True), source="ui")
    adapter.topology.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [{"ieee_address": "0x0000000000000002", "endpoint": 1}],
            }
        ]
    )
    adapter.topology_callback(adapter.topology.version)
    assert adapter.active
    assert instance.mode == ControlMode.ACTIVE
    assert instance.snapshot("light_a").desired.on
    assert not instance.group_snapshot("group_a").available
    with pytest.raises(RuntimeError):
        instance.submit_group("group_a", IntentPatch(on=True), source="ui")
    instance.submit(("light_b",), IntentPatch(on=True), source="ui")


async def test_identical_catalog_callback_keeps_authority_without_replay(runtime):
    instance, adapter = runtime
    instance.set_mode(ControlMode.ACTIVE)
    instance.submit(("light_a",), IntentPatch(on=True), source="ui")
    sent = list(adapter.sent)
    adapter.topology_callback(adapter.topology.version)
    assert adapter.active
    assert instance.mode == ControlMode.ACTIVE
    assert instance.snapshot("light_a").desired.on
    assert adapter.sent == sent
    assert instance.diagnostics()["topology_stale"] is False


async def test_bridge_offline_revokes_mode_and_does_not_resume(runtime):
    from custom_components.zigbee_led_orchestrator.models import (
        BridgeObservation,
        EvidenceQuality,
    )

    instance, adapter = runtime
    instance.set_mode(ControlMode.ACTIVE)
    adapter.observation(
        BridgeObservation(False, 1.0, False, EvidenceQuality.UNKNOWN_SOURCE)
    )
    assert instance.mode == ControlMode.OBSERVE
    assert not adapter.active
    sent = list(adapter.sent)
    adapter.observation(
        BridgeObservation(True, 2.0, False, EvidenceQuality.UNKNOWN_SOURCE)
    )
    assert instance.mode == ControlMode.ACTIVE
    assert adapter.active
    assert not instance.snapshot("light_a").desired.on
    assert adapter.sent == sent
    instance.submit(("light_a",), IntentPatch(on=True), source="ui")


async def test_unknown_capabilities_reject_brightness_before_changing_intent(runtime):
    instance, _ = runtime
    instance.set_mode(ControlMode.ACTIVE)
    with pytest.raises(ValueError):
        instance.submit(("light_a",), IntentPatch(on=True, brightness=100), source="ui")
    assert not instance.snapshot("light_a").desired.on
