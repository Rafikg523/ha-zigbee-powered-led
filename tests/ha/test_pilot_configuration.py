"""Pilot setup uses real catalog capabilities, with no mandatory broadcast."""

import pytest

from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow
from tests.unit.test_topology_config import topology


def selections(**changes):
    return {
        "light_1": "0x0000000000000002|1",
        "light_2": "0x0000000000000003|1",
        "relay_1": "0x0000000000000001|1|state_l1",
        "relay_2": "0x0000000000000001|2|state_l2",
        "group_name": "",
        "min_send_interval_ms": 100,
        "publication_timeout_ms": 3000,
        **changes,
    }


@pytest.fixture
async def flow(hass, monkeypatch):
    catalog = topology()
    from types import SimpleNamespace

    from tests.ha.test_preferences import enable_brightness

    enable_brightness(SimpleNamespace(topology=catalog))

    async def discover(hass, base_topic):
        return catalog

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    instance = ConfigFlow()
    instance.hass = hass
    await instance.async_step_user({"base_topic": "zigbee2mqtt"})
    return instance


async def test_configure_two_lights_without_group_and_infer_temperature(flow):
    result = await flow.async_step_mapping(selections())
    assert result["type"] == "create_entry"
    assert result["data"]["groups"] == []
    assert result["data"]["lights"][0]["min_kelvin"] == 2000
    assert result["data"]["lights"][0]["max_kelvin"] == 6536


async def test_mapping_only_offers_light_endpoints(flow):
    form = await flow.async_step_mapping()
    schema = form["data_schema"].schema
    selector = next(value for key, value in schema.items() if str(key) == "light_1")
    assert "0x0000000000000001|1" not in selector.container
    assert "0x0000000000000002|1" in selector.container


async def test_options_remain_accessible_before_entry_is_loaded(hass):
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.zigbee_led_orchestrator.const import DOMAIN
    from tests.unit.test_configuration import valid_config

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"active": True}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "control_unavailable"


async def test_setup_accepts_explicit_pilot_timing(flow):
    result = await flow.async_step_mapping(
        selections(boot_grace_ms=4000, readiness_timeout_ms=12000)
    )
    assert result["type"] == "create_entry"
    assert result["data"]["policies"]["boot_grace_ms"] == 4000
    assert result["data"]["policies"]["readiness_timeout_ms"] == 12000
