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
        "light_1",
        "light_2",
        "relay_1",
        "relay_2",
        "group",
    }
    result = await flow.async_step_mapping(
        {
            "light_1": "0x0000000000000002|1",
            "light_2": "0x0000000000000003|1",
            "relay_1": "0x0000000000000001|1|state_l1",
            "relay_2": "0x0000000000000001|2|state_l2",
            "group": "1",
            "min_kelvin_1": 2000,
            "max_kelvin_1": 6500,
            "min_kelvin_2": 2000,
            "max_kelvin_2": 6500,
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
        }
    )
    assert result["type"] == "create_entry"
    assert result["data"]["lights"][0]["device_ieee"] == "0x0000000000000002"
