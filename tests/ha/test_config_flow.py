"""Config flow tests with synthetic discovery catalogs."""

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zigbee_led_orchestrator.const import DOMAIN
from tests.unit.test_configuration import valid_config
from tests.unit.test_topology_config import topology


@pytest.fixture
def discovery(monkeypatch):
    async def discover(hass, base_topic):
        return topology()

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )


async def test_invalid_mapping_does_not_create_entry(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user({"base_topic": "zigbee2mqtt"})
    assert result["type"] == "form"
    data = valid_config()
    data["lights"][0]["endpoint"] = 12
    result = await flow.async_step_mapping(data)
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_mapping"


async def test_duplicate_entry_aborts(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user({"base_topic": "another"})
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance"


async def test_flow_accepts_matching_ieee_endpoints_and_group(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    await flow.async_step_user({"base_topic": "zigbee2mqtt"})
    result = await flow.async_step_mapping({"configuration": valid_config()})
    assert result["type"] == "create_entry"
    assert result["data"]["lights"][0]["device_ieee"] == "0x0000000000000002"
    assert result["data"]["groups"][0]["z2m_id"] == 1


async def test_discovery_uses_retained_metadata_and_unsubscribes(hass, monkeypatch):
    import json
    from types import SimpleNamespace

    from homeassistant.components import mqtt

    from custom_components.zigbee_led_orchestrator.config_flow import discover_topology
    from tests.ha.test_mqtt_adapter import devices

    removed = []

    async def subscribe(hass, topic, callback):
        payload = devices() if topic.endswith("devices") else []
        callback(SimpleNamespace(topic=topic, payload=json.dumps(payload)))
        return lambda: removed.append(topic)

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    catalog = await discover_topology(hass, "zigbee2mqtt")
    assert (
        catalog.resolve_device("0x0000000000000001").topic == "zigbee2mqtt/room/relay"
    )
    assert len(removed) == 2


async def test_flow_is_registered_with_home_assistant(
    hass, discovery, enable_custom_integrations
):
    from homeassistant import data_entry_flow

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_flow_presents_catalog_choices_and_constructs_mapping(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    form = await flow.async_step_user({"base_topic": "zigbee2mqtt"})
    assert {str(key) for key in form["data_schema"].schema} >= {
        "name",
        "light",
        "relay",
        "min_send_interval_ms",
        "publication_timeout_ms",
        "boot_grace_ms",
        "readiness_timeout_ms",
    }
    result = await flow.async_step_mapping(
        {
            "name": "Kuchnia",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|1|state_l1",
            "min_kelvin": 2000,
            "max_kelvin": 6500,
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
        }
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Zigbee LED Orchestrator"
    assert result["data"]["lights"] == [
        {
            "id": "light_1",
            "name": "Kuchnia",
            "device_ieee": "0x0000000000000002",
            "endpoint": 1,
            "power_channel": "relay_1",
            "min_kelvin": 2000,
            "max_kelvin": 6500,
        }
    ]
    assert result["data"]["groups"] == []


async def test_blank_name_is_rejected(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    await flow.async_step_user({"base_topic": "zigbee2mqtt"})
    result = await flow.async_step_mapping(
        {
            "name": "  ",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|1|state_l1",
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
        }
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_name"


def _one_pair():
    return {
        "base_topic": "zigbee2mqtt",
        "relay_channels": [
            {
                "id": "relay_1",
                "device_ieee": "0x0000000000000001",
                "endpoint": 1,
                "state_key": "state_l1",
            }
        ],
        "lights": [
            {
                "id": "light_1",
                "name": "Kuchnia",
                "device_ieee": "0x0000000000000002",
                "endpoint": 1,
                "power_channel": "relay_1",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            }
        ],
        "groups": [],
        "rooms": [],
        "policies": {
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
            "boot_grace_ms": 2500,
            "readiness_timeout_ms": 8000,
        },
        "report_extension": False,
    }


async def test_options_add_circuit_reuses_policies_and_reloads(
    hass, discovery, monkeypatch
):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    entry = MockConfigEntry(domain=DOMAIN, data=_one_pair(), options={"active": False})
    entry.add_to_hass(hass)
    scheduled = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_schedule_reload",
        lambda entry_id: scheduled.append(entry_id),
    )
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert menu["menu_options"] == ["control", "add_circuit"]
    form = await flow.async_step_add_circuit()
    assert {str(key) for key in form["data_schema"].schema} == {
        "name",
        "light",
        "relay",
    }
    selector = next(
        value
        for key, value in form["data_schema"].schema.items()
        if str(key) == "light"
    )
    assert "0x0000000000000002|1" not in selector.container
    assert "0x0000000000000003|1" in selector.container
    taken = await flow.async_step_add_circuit(
        {
            "name": "Salon",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|2|state_l2",
        }
    )
    assert taken["type"] == "form"
    assert taken["errors"]["base"] == "already_mapped"
    result = await flow.async_step_add_circuit(
        {
            "name": "Salon",
            "light": "0x0000000000000003|1",
            "relay": "0x0000000000000001|2|state_l2",
        }
    )
    assert result["type"] == "create_entry"
    assert entry.data["policies"] == _one_pair()["policies"]
    assert entry.data["lights"][1]["id"] == "light_2"
    assert entry.data["lights"][1]["name"] == "Salon"
    assert entry.data["lights"][0]["name"] == "Kuchnia"
    assert entry.data["groups"] == []
    assert scheduled == [entry.entry_id]


async def test_options_reject_duplicate_name_and_full_catalog(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    entry = MockConfigEntry(domain=DOMAIN, data=_one_pair())
    entry.add_to_hass(hass)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_add_circuit(
        {
            "name": "Kuchnia",
            "light": "0x0000000000000003|1",
            "relay": "0x0000000000000001|2|state_l2",
        }
    )
    assert result["errors"]["base"] == "already_mapped"
    full = MockConfigEntry(domain=DOMAIN, data=valid_config())
    full.add_to_hass(hass)
    blocked = OptionsFlow()
    blocked.hass = hass
    blocked.handler = full.entry_id
    aborted = await blocked.async_step_add_circuit()
    assert aborted["type"] == "abort"
    assert aborted["reason"] == "no_free_circuit"
