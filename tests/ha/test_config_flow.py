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


async def test_invalid_base_topic_does_not_create_entry(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user({"base_topic": "/zigbee2mqtt"})
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


async def test_install_creates_an_empty_entry(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        {
            "base_topic": "zigbee2mqtt",
            "report_extension": True,
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
        }
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Zigbee LED Orchestrator"
    assert result["data"]["lights"] == []
    assert result["data"]["groups"] == []
    assert result["data"]["relay_channels"] == []
    assert result["data"]["report_extension"] is True


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
    form = await flow.async_step_user()
    assert {str(key) for key in form["data_schema"].schema} >= {
        "base_topic",
        "report_extension",
        "min_send_interval_ms",
        "publication_timeout_ms",
        "boot_grace_ms",
        "readiness_timeout_ms",
    }
    assert "light" not in {str(key) for key in form["data_schema"].schema}
    assert "group_name" not in {str(key) for key in form["data_schema"].schema}


async def test_blank_name_is_rejected(hass, discovery):
    from custom_components.zigbee_led_orchestrator.config_flow import (
        ConfigFlow,
        OptionsFlow,
    )

    flow = ConfigFlow()
    flow.hass = hass
    created = await flow.async_step_user(
        {
            "base_topic": "zigbee2mqtt",
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
        }
    )
    entry = MockConfigEntry(domain=DOMAIN, data=created["data"])
    entry.add_to_hass(hass)
    options = OptionsFlow()
    options.hass = hass
    options.handler = entry.entry_id
    result = await options.async_step_add_circuit(
        {
            "name": "  ",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|1|state_l1",
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
    hass.config.language = "en"
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert menu["step_id"] == "choose"
    assert menu["menu_options"] == {
        "add_circuit": "Add circuit",
        "create_group": "Create group",
        "edit_circuit": "Edit circuit",
        "edit_group": "Edit group",
    }
    form = await flow.async_step_add_circuit()
    assert {str(key) for key in form["data_schema"].schema} == {
        "name",
        "light",
        "relay",
    }
    lights = _choice_labels(form, "light")
    relays = _choice_labels(form, "relay")
    assert "0x0000000000000002|1" not in lights
    assert "zigbee2mqtt/led2 — 0x0000000000000003 / 1" in lights
    assert "zigbee2mqtt/relay — 0x0000000000000001 / 2 / state_l2" in relays
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


async def test_typed_name_must_be_a_listed_choice(hass, discovery, monkeypatch):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "base_topic": "zigbee2mqtt",
            "relay_channels": [],
            "lights": [],
            "groups": [],
            "rooms": [],
            "policies": _one_pair()["policies"],
            "report_extension": False,
        },
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", lambda entry_id: None
    )
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_add_circuit()
    light = next(
        item for item in _choice_labels(form, "light") if "0x0000000000000002" in item
    )
    relay = next(
        item for item in _choice_labels(form, "relay") if item.endswith("state_l1")
    )
    fragment = await flow.async_step_add_circuit(
        {"name": "Kuchnia", "light": "led1", "relay": relay}
    )
    assert fragment["type"] == "form"
    assert fragment["errors"]["base"] == "unknown_choice"
    result = await flow.async_step_add_circuit(
        {"name": "Kuchnia", "light": light, "relay": relay}
    )
    assert result["type"] == "create_entry"
    assert entry.data["lights"][0]["device_ieee"] == "0x0000000000000002"
    assert entry.data["lights"][0]["endpoint"] == 1
    assert entry.data["relay_channels"][0]["state_key"] == "state_l1"


def _choice_labels(form, field: str) -> list[str]:
    selector = next(
        value for key, value in form["data_schema"].schema.items() if str(key) == field
    )
    assert selector.config["mode"] == "dropdown"
    assert selector.config["custom_value"] is True
    assert selector.config["multiple"] is False
    options = selector.config["options"]
    assert options
    assert all(item["value"] == item["label"] for item in options)
    return [item["value"] for item in options]


def test_menu_labels_follow_the_ha_language():
    from custom_components.zigbee_led_orchestrator.config_flow import menu_options_for

    assert menu_options_for("pl") == {
        "add_circuit": "Dodaj obwód",
        "create_group": "Utwórz grupę",
        "edit_circuit": "Edytuj obwód",
        "edit_group": "Edytuj grupę",
    }
    assert list(menu_options_for("de")) == [
        "add_circuit",
        "create_group",
        "edit_circuit",
        "edit_group",
    ]


def _two_pairs():
    data = _one_pair()
    data["relay_channels"].append(
        {
            "id": "relay_2",
            "device_ieee": "0x0000000000000001",
            "endpoint": 2,
            "state_key": "state_l2",
        }
    )
    data["lights"].append(
        {
            "id": "light_2",
            "name": "Jadalnia",
            "device_ieee": "0x0000000000000003",
            "endpoint": 1,
            "power_channel": "relay_2",
            "min_kelvin": 2000,
            "max_kelvin": 6500,
        }
    )
    return data


async def test_added_circuit_stays_out_of_existing_groups(hass, discovery, monkeypatch):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    data = _one_pair()
    data["groups"] = [
        {"id": "group_1", "name": "Salon", "z2m_id": 7, "members": ["light_1"]}
    ]
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", lambda entry_id: None
    )
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_add_circuit(
        {
            "name": "Jadalnia",
            "light": "0x0000000000000003|1",
            "relay": "0x0000000000000001|2|state_l2",
        }
    )
    assert result["type"] == "create_entry"
    assert entry.data["groups"] == [
        {"id": "group_1", "name": "Salon", "z2m_id": 7, "members": ["light_1"]}
    ]


async def test_create_group_keeps_only_selected_circuits(hass, discovery, monkeypatch):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    data = _two_pairs()
    data["relay_channels"].append(
        {
            "id": "relay_3",
            "device_ieee": "0x0000000000000004",
            "endpoint": 1,
            "state_key": "state_l3",
        }
    )
    data["lights"].append(
        {
            "id": "light_3",
            "name": "Korytarz",
            "device_ieee": "0x0000000000000005",
            "endpoint": 1,
            "power_channel": "relay_3",
            "min_kelvin": 2000,
            "max_kelvin": 6500,
        }
    )
    entry = MockConfigEntry(domain=DOMAIN, data=data)
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
    blocked = await flow.async_step_create_group(
        {"name": "Salon", "members": ["light_1"]}
    )
    assert blocked["type"] == "form"
    assert blocked["errors"]["base"] == "need_two_members"
    result = await flow.async_step_create_group(
        {"name": "Salon", "members": ["light_2", "light_1"]}
    )
    assert result["type"] == "create_entry"
    assert entry.data["groups"] == [
        {"id": "group_1", "name": "Salon", "members": ["light_1", "light_2"]}
    ]
    assert "light_3" not in entry.data["groups"][0]["members"]
    assert scheduled == [entry.entry_id]
    again = OptionsFlow()
    again.hass = hass
    again.handler = entry.entry_id
    aborted = await again.async_step_create_group()
    assert aborted["type"] == "abort"
    assert aborted["reason"] == "need_two_circuits"


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
