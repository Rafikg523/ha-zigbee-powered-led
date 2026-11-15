"""Pilot setup uses real catalog capabilities, with no mandatory broadcast."""

import pytest

from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow
from tests.unit.test_topology_config import topology


def selections(**changes):
    return {
        "base_topic": "zigbee2mqtt",
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
    return instance


async def test_configure_one_light_and_infer_temperature(hass, flow, monkeypatch):
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow
    from custom_components.zigbee_led_orchestrator.const import DOMAIN

    created = await flow.async_step_user(selections())
    entry = MockConfigEntry(domain=DOMAIN, data=created["data"])
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", lambda entry_id: None
    )
    options = OptionsFlow()
    options.hass = hass
    options.handler = entry.entry_id
    result = await options.async_step_add_circuit(
        {
            "name": "Kuchnia",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|1|state_l1",
        }
    )
    assert result["type"] == "create_entry"
    assert entry.data["groups"] == []
    assert entry.data["lights"][0]["name"] == "Kuchnia"
    assert entry.data["lights"][0]["min_kelvin"] == 2000
    assert entry.data["lights"][0]["max_kelvin"] == 6536


async def test_add_circuit_only_offers_light_endpoints(hass, flow):
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow
    from custom_components.zigbee_led_orchestrator.const import DOMAIN
    from tests.ha.test_config_flow import _choice_labels

    created = await flow.async_step_user(selections())
    entry = MockConfigEntry(domain=DOMAIN, data=created["data"])
    entry.add_to_hass(hass)
    options = OptionsFlow()
    options.hass = hass
    options.handler = entry.entry_id
    form = await options.async_step_add_circuit()
    lights = _choice_labels(form, "light")
    relays = _choice_labels(form, "relay")
    assert "0x0000000000000001|1" not in lights
    assert "zigbee2mqtt/led1 — 0x0000000000000002 / 1" in lights
    assert any(item.endswith("state_l1") for item in relays)


async def test_options_remain_accessible_before_entry_is_loaded(hass):
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.zigbee_led_orchestrator.const import DOMAIN
    from tests.unit.test_configuration import valid_config

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert set(result["menu_options"]) == {
        "add_circuit",
        "create_group",
        "edit_circuit",
        "edit_group",
    }


async def test_setup_accepts_explicit_pilot_timing(flow):
    result = await flow.async_step_user(
        selections(boot_grace_ms=4000, readiness_timeout_ms=12000)
    )
    assert result["type"] == "create_entry"
    assert result["data"]["policies"]["boot_grace_ms"] == 4000
    assert result["data"]["policies"]["readiness_timeout_ms"] == 12000
    assert result["data"]["lights"] == []
