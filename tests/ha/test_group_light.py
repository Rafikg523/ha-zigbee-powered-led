"""Group light entity and runtime wiring using synthetic data only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.light_capabilities import (
    LightCapabilities,
)
from custom_components.zigbee_led_orchestrator.models import (
    ControlMode,
    DesiredState,
    EffectiveState,
    EvidenceQuality,
    GroupSnapshot,
    IntentPatch,
    LightSnapshot,
    Phase,
    RelayObservation,
    SwitchState,
)
from tests.ha.test_runtime import FakeAdapter
from tests.unit.test_configuration import valid_config


class GroupRuntimeStub:
    def __init__(self):
        self.desired = {
            item: DesiredState(False, None, None, 0, "initial", 0.0)
            for item in ("light_a", "light_b")
        }
        self.available = True
        self.patches = []
        self.capabilities = LightCapabilities((1, 254), (153, 500))

    def snapshot(self, light_id):
        return LightSnapshot(
            self.desired[light_id],
            Phase.POWERING,
            EffectiveState.UNKNOWN,
            False,
            self.available,
            None,
        )

    def group_snapshot(self, group_id):
        return GroupSnapshot(
            any(state.on for state in self.desired.values()),
            0,
            2,
            False,
            self.available,
        )

    def group_capabilities(self, group_id):
        return self.capabilities

    def submit_group(self, group_id, patch, *, source):
        if not self.available:
            raise RuntimeError("control unavailable")
        self.patches.append((group_id, patch, source))
        for item, state in self.desired.items():
            self.desired[item] = replace(
                state,
                on=state.on if patch.on is None else patch.on,
                brightness=patch.brightness or state.brightness,
                color_temp_kelvin=patch.color_temp_kelvin or state.color_temp_kelvin,
            )

    def subscribe(self, callback):
        self.callback = callback
        return lambda: None


@pytest.fixture
def group():
    from custom_components.zigbee_led_orchestrator.light import OrchestratorGroupLight

    runtime = GroupRuntimeStub()
    entity = OrchestratorGroupLight(
        runtime, "entry-123", "group_a", ("light_a", "light_b"), 2000, 6500
    )
    entity._refresh_capabilities()
    return entity, runtime


async def test_group_commands_use_one_group_operation(group):
    entity, runtime = group
    await entity.async_turn_on(brightness=120, color_temp_kelvin=3000)
    assert runtime.patches == [
        (
            "group_a",
            IntentPatch(on=True, brightness=120, color_temp_kelvin=3000),
            "home_assistant",
        )
    ]
    assert entity.is_on
    assert entity.brightness == 120
    await entity.async_turn_off()
    assert runtime.patches[-1][1] == IntentPatch(on=False)
    assert not entity.is_on


def test_group_is_on_when_any_member_is_requested_on(group):
    entity, runtime = group
    runtime.desired["light_b"] = replace(runtime.desired["light_b"], on=True)
    assert entity.is_on
    assert entity.brightness is None


def test_group_attributes_report_member_confirmation_separately(group):
    entity, _ = group
    assert entity.extra_state_attributes == {
        "members": ["light_a", "light_b"],
        "confirmed_members": 0,
        "member_count": 2,
        "confirmed": False,
    }


def test_group_unique_id_does_not_depend_on_name(group):
    entity, _ = group
    assert entity.unique_id == "entry-123_group_a"


async def test_unavailable_group_control_is_rejected(group):
    entity, runtime = group
    runtime.available = False
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()


async def test_group_capabilities_are_intersection_of_members(group):
    entity, runtime = group
    runtime.capabilities = LightCapabilities(None, (153, 500))
    entity._refresh_capabilities()
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on(brightness=10)
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on(color_temp_kelvin=3000)


async def test_platform_creates_group_entity_next_to_lights():
    from custom_components.zigbee_led_orchestrator.light import async_setup_entry

    runtime = GroupRuntimeStub()
    runtime.config = validate_config(valid_config())
    entities = []
    entry = SimpleNamespace(entry_id="entry-123", runtime_data=runtime)
    hass = SimpleNamespace(config=SimpleNamespace(language="pl"))
    await async_setup_entry(hass, entry, entities.extend)
    assert {entity.unique_id for entity in entities} == {
        "entry-123_light_a",
        "entry-123_light_b",
        "entry-123_group_a",
    }
    group_entity = next(
        entity for entity in entities if entity.unique_id.endswith("group_a")
    )
    assert group_entity.name is None
    assert group_entity.device_info["name"] == "group_a_orchestrator"
    assert group_entity.device_info["model"] == "Grupa obwodów"
    assert group_entity.device_info["identifiers"] == {
        ("zigbee_led_orchestrator", "entry-123_group_a")
    }


@pytest.fixture
async def runtime():
    from custom_components.zigbee_led_orchestrator.runtime import Runtime

    adapter = FakeAdapter()
    instance = Runtime(validate_config(valid_config()), adapter)
    await instance.start()
    yield instance, adapter
    await instance.close()


async def test_runtime_group_submit_requires_active_mode(runtime):
    instance, adapter = runtime
    with pytest.raises(RuntimeError):
        instance.submit_group("group_a", IntentPatch(on=True), source="ui")
    assert not adapter.sent


async def test_runtime_group_on_powers_each_member_and_never_sends_group_early(
    runtime,
):
    import asyncio

    instance, adapter = runtime
    for channel in ("relay_a", "relay_b"):
        adapter.observation(
            RelayObservation(
                channel, SwitchState.OFF, 0.0, False, EvidenceQuality.DEVICE_REPORT
            )
        )
    instance.set_mode(ControlMode.ACTIVE)
    instance.submit_group("group_a", IntentPatch(on=True), source="ui")
    await asyncio.sleep(0.3)
    assert [payload for _, payload, _ in adapter.sent] == [{"state_l1": "ON"}]
    adapter.observation(
        RelayObservation(
            "relay_a",
            SwitchState.ON,
            instance._clock.now(),
            False,
            EvidenceQuality.DEVICE_REPORT,
        )
    )
    await asyncio.sleep(0.15)
    assert sorted(next(iter(payload.items())) for _, payload, _ in adapter.sent) == [
        ("state_l1", "ON"),
        ("state_l2", "ON"),
    ]
    assert all(topic == "zigbee2mqtt/relay/set" for topic, _, _ in adapter.sent)
    snapshot = instance.group_snapshot("group_a")
    assert snapshot.on and not snapshot.confirmed


async def test_runtime_topology_change_revokes_group_control(runtime):
    instance, adapter = runtime
    instance.set_mode(ControlMode.ACTIVE)
    instance.submit_group("group_a", IntentPatch(on=True), source="ui")
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
    assert not instance.group_snapshot("group_a").on
    with pytest.raises(RuntimeError):
        instance.submit_group("group_a", IntentPatch(on=True), source="ui")


async def test_runtime_group_capabilities_are_member_intersection(runtime):
    instance, _ = runtime
    assert instance.group_capabilities("group_a") == LightCapabilities(None, None)
