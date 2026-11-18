"""HA integration tests for N groups, M circuits, and Z2M sync without DEVICE_REPORT."""

# ruff: noqa: F811

import copy

from pytest_homeassistant.common import MockConfigEntry

from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow
from custom_components.zigbee_led_orchestrator.const import DOMAIN
from custom_components.zigbee_led_orchestrator.models import (
    BridgeObservation,
    EvidenceQuality,
    RelayObservation,
    SwitchState,
)
from tests.ha.test_init import mocked_adapter  # noqa: F401
from tests.ha.test_services import setup  # noqa: F401
from tests.unit.test_multi_group_scale import make_scale_config, make_scale_topology


async def test_add_third_circuit_with_single_gang_relay(hass, monkeypatch):
    """Verify adding a 3rd circuit with state_key='state' succeeds and preserves config."""
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    base_data = {
        "base_topic": "zigbee2mqtt",
        "relay_channels": [
            {
                "id": "relay_1",
                "device_ieee": "0x0000000000000001",
                "endpoint": 1,
                "state_key": "state_l1",
            },
            {
                "id": "relay_2",
                "device_ieee": "0x0000000000000001",
                "endpoint": 2,
                "state_key": "state_l2",
            },
        ],
        "lights": [
            {
                "id": "light_1",
                "name": "Light 1",
                "device_ieee": "0x0000000000000002",
                "endpoint": 1,
                "power_channel": "relay_1",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            },
            {
                "id": "light_2",
                "name": "Light 2",
                "device_ieee": "0x0000000000000003",
                "endpoint": 1,
                "power_channel": "relay_2",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            },
        ],
        "groups": [],
        "rooms": [],
        "policies": {
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
            "boot_grace_ms": 1000,
            "readiness_timeout_ms": 5000,
        },
        "report_extension": False,
    }

    # Third circuit uses a 1-gang relay with property 'state'
    topo_data = copy.deepcopy(base_data)
    topo_data["relay_channels"].append(
        {
            "id": "relay_3",
            "device_ieee": "0x0000000000000004",
            "endpoint": 1,
            "state_key": "state",
        }
    )
    topo_data["lights"].append(
        {
            "id": "light_3",
            "name": "Light 3",
            "device_ieee": "0x0000000000000005",
            "endpoint": 1,
            "power_channel": "relay_3",
            "min_kelvin": 2000,
            "max_kelvin": 6500,
        }
    )
    topo = make_scale_topology(topo_data)

    async def discover(h, b):
        return topo

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )

    entry = MockConfigEntry(domain=DOMAIN, data=base_data)
    entry.add_to_hass(hass)

    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id

    result = await flow.async_step_add_circuit(
        {
            "name": "Light 3",
            "light": "0x0000000000000005|1",
            "relay": "0x0000000000000004|1|state",
        }
    )
    assert result["type"] == "create_entry"
    assert len(entry.data["lights"]) == 3
    assert len(entry.data["relay_channels"]) == 3
    assert entry.data["lights"][2]["id"] == "light_3"
    assert entry.data["lights"][2]["name"] == "Light 3_orchestrator"
    assert entry.data["relay_channels"][2]["state_key"] == "state"


async def test_create_multiple_groups_with_arbitrary_circuits(hass, monkeypatch):
    """Verify creating 2 independent groups with multiple circuits."""
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    scale_raw = make_scale_config(num_groups=2, circuits_per_group=3)
    scale_raw["groups"] = []  # Start with no groups

    async def discover(_hass, _base):
        return make_scale_topology(scale_raw)

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    entry = MockConfigEntry(domain=DOMAIN, data=scale_raw)
    entry.add_to_hass(hass)

    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id

    # Create Group 1 with light_1, light_2, light_3
    res1 = await flow.async_step_create_group(
        {"name": "Group Alpha", "members": ["light_1", "light_2", "light_3"]}
    )
    assert res1["type"] == "create_entry"
    assert len(entry.data["groups"]) == 1
    assert entry.data["groups"][0]["name"] == "Group Alpha"
    assert entry.data["groups"][0]["members"] == ["light_1", "light_2", "light_3"]

    # Create Group 2 with remaining light_4, light_5, light_6
    flow2 = OptionsFlow()
    flow2.hass = hass
    flow2.handler = entry.entry_id
    res2 = await flow2.async_step_create_group(
        {"name": "Group Beta", "members": ["light_4", "light_5", "light_6"]}
    )
    assert res2["type"] == "create_entry"
    assert len(entry.data["groups"]) == 2
    assert entry.data["groups"][1]["name"] == "Group Beta"
    assert entry.data["groups"][1]["members"] == ["light_4", "light_5", "light_6"]


async def test_provisioning_without_device_report(setup):
    """Verify that a relay reported ON via standard MQTT (UNKNOWN_SOURCE) allows group member addition."""
    _hass, _entry, runtime, adapter = setup
    runtime._options_ready.add("group_a_orchestrator")
    adapter.topology.update_groups(
        [{"id": 1, "friendly_name": "group_a_orchestrator", "members": []}]
    )
    # Initially relays are OFF so powered_lights is empty
    assert len(runtime._powered_lights()) == 0

    # Ingest relay_a ON with EvidenceQuality.UNKNOWN_SOURCE (normal MQTT message without report_extension)
    now = runtime._clock.now() + 1.0
    adapter.observation(
        RelayObservation(
            "relay_a", SwitchState.ON, now, False, EvidenceQuality.UNKNOWN_SOURCE
        )
    )

    # _relay_powered now recognizes relay_a is ON
    assert runtime._relay_powered("relay_a") is True
    assert "light_a" in runtime._powered_lights()

    # Plan action should now plan adding light_a to group_a_orchestrator
    action = runtime._plan_action()
    assert action is not None
    assert action.path == "group/members/add"
    assert action.payload["group"] == "group_a_orchestrator"
    assert action.payload["endpoint"] == 1


async def test_multi_group_setup_in_home_assistant(
    hass, mqtt_mock, mocked_adapter, enable_custom_integrations
):
    """Verify that multiple groups and circuits set up independent HA light entities."""
    raw = make_scale_config(num_groups=5, circuits_per_group=10)
    topo = make_scale_topology(raw)
    entry = MockConfigEntry(domain=DOMAIN, data=raw, options={"active": True})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    adapter = mocked_adapter[0]
    adapter.topology = topo
    adapter.observation(
        BridgeObservation(
            True, entry.runtime_data._clock.now(), False, EvidenceQuality.UNKNOWN_SOURCE
        )
    )
    adapter.topology_callback(topo.version)
    await hass.async_block_till_done()

    # Check that each group has an entity in HA
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for g in raw["groups"]:
        entity_id = registry.async_get_entity_id(
            "light", DOMAIN, f"{entry.entry_id}_{g['id']}"
        )
        assert entity_id is not None, f"Entity {g['id']} was not created"
        state = hass.states.get(entity_id)
        assert state is not None, f"Entity {entity_id} was not created"
        assert state.attributes["member_count"] == 10
    for light in raw["lights"]:
        assert hass.states.get(f"light.{light['id']}") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def test_create_single_member_group(hass, monkeypatch):
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    raw = make_scale_config(num_groups=1, circuits_per_group=1)
    raw["groups"] = []

    async def discover(_hass, _base):
        return make_scale_topology(raw)

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    entry = MockConfigEntry(domain=DOMAIN, data=raw)
    entry.add_to_hass(hass)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_create_group(
        {"name": "Desk", "members": ["light_1"]}
    )
    assert result["type"] == "create_entry"
    assert entry.data["groups"] == [
        {"id": "group_1", "name": "Desk", "members": ["light_1"]}
    ]


async def test_create_group_does_not_adopt_a_manual_group(hass, monkeypatch):
    raw = make_scale_config(num_groups=1, circuits_per_group=2)
    topology = make_scale_topology(raw)
    raw["groups"] = []

    async def discover(_hass, _base):
        return topology

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    entry = MockConfigEntry(domain=DOMAIN, data=raw)
    entry.add_to_hass(hass)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_create_group(
        {"name": "Room 1", "members": ["light_1", "light_2"]}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "already_mapped"
    assert entry.data["groups"] == []
