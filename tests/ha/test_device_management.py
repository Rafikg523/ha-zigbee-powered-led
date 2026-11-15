"""Circuits and groups as devices: listing, deleting and editing them."""

import copy

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zigbee_led_orchestrator.const import DOMAIN
from tests.ha.test_runtime import FakeAdapter
from tests.unit.test_configuration import valid_config

GROUP = {
    "id": 1,
    "friendly_name": "group_a_orchestrator",
    "members": [
        {"ieee_address": "0x0000000000000002", "endpoint": 1},
        {"ieee_address": "0x0000000000000003", "endpoint": 1},
    ],
}


class BridgeAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.topology.update_groups([GROUP])
        self.requests = []
        self.response = {"status": "ok"}

    async def request_bridge(self, path, payload):
        self.requests.append((path, payload))
        if self.response.get("status") != "ok":
            return self.response
        if path == "group/options":
            return {"status": "ok", "data": {"to": payload["options"]}}
        if path == "group/remove":
            self.topology.update_groups([])
            if self.topology_callback:
                self.topology_callback(self.topology.version)
            return {"status": "ok", "data": {"id": payload["id"], "force": False}}
        if path == "group/rename":
            group = copy.deepcopy(GROUP)
            group["friendly_name"] = payload["to"]
            self.topology.update_groups([group])
            if self.topology_callback:
                self.topology_callback(self.topology.version)
            return {"status": "ok", "data": payload}
        pytest.fail(f"Unexpected bridge request: {path}")


@pytest.fixture
def adapters(monkeypatch):
    created = []

    def factory(hass, config):
        created.append(BridgeAdapter())
        return created[-1]

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.mqtt_adapter.MqttAdapter", factory
    )
    return created


@pytest.fixture
async def loaded(hass, mqtt_mock, adapters, enable_custom_integrations):
    hass.config.language = "pl"
    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "light",
        DOMAIN,
        f"{entry.entry_id}_light_a",
        config_entry=entry,
        suggested_object_id="kuchnia_custom",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from custom_components.zigbee_led_orchestrator.models import (
        BridgeObservation,
        EvidenceQuality,
    )

    adapters[0].observation(
        BridgeObservation(
            True, entry.runtime_data._clock.now(), False, EvidenceQuality.UNKNOWN_SOURCE
        )
    )
    await hass.async_block_till_done()
    task = entry.runtime_data._provision_task
    if task is not None:
        await task
    adapters[0].requests.clear()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


def _device(hass, entry, item_id):
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_{item_id}"), entry.entry_id
    )


async def test_entry_lists_a_device_per_circuit_and_group(hass, loaded):
    entry = loaded
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert {(d.name, d.model) for d in devices} == {
        ("Light A", "Obwód LED"),
        ("Light B", "Obwód LED"),
        ("group_a_orchestrator", "Grupa obwodów"),
    }
    registry = er.async_get(hass)
    circuit = _device(hass, entry, "light_a")
    attached = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in er.async_entries_for_device(registry, circuit.id)
    }
    assert attached == {
        "light_a",
        "light_a_phase",
        "light_a_effective_state",
        "light_a_confirmed",
    }
    group = _device(hass, entry, "group_a")
    assert [
        item.unique_id for item in er.async_entries_for_device(registry, group.id)
    ] == [f"{entry.entry_id}_group_a"]
    assert registry.async_get_entity_id(
        "light", DOMAIN, f"{entry.entry_id}_light_a"
    ) == ("light.kuchnia_custom")
    assert hass.states.get("light.kuchnia_custom").name == "Light A"
    phase = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_light_a_phase"
    )
    assert hass.states.get(phase).name == "Light A Faza"


async def test_grouped_circuit_delete_is_refused_over_websocket(
    hass, loaded, hass_ws_client
):
    entry = loaded
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": _device(hass, entry, "light_a").id,
        }
    )
    response = await client.receive_json()
    assert not response["success"]
    assert "group_a" in response["error"]["message"]
    assert entry.data == valid_config()
    assert _device(hass, entry, "light_a") is not None


async def test_group_delete_waits_for_zigbee2mqtt_then_drops_it(
    hass, loaded, adapters, hass_ws_client, monkeypatch
):
    entry = loaded
    scheduled = []
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", scheduled.append)
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": _device(hass, entry, "group_a").id,
        }
    )
    response = await client.receive_json()
    assert response["success"]
    assert adapters[0].requests == [("group/remove", {"id": 1})]
    assert entry.data["groups"] == []
    assert entry.data["rooms"] == []
    assert len(entry.data["lights"]) == 2
    assert _device(hass, entry, "group_a") is None
    assert (
        er.async_get(hass).async_get_entity_id(
            "light", DOMAIN, f"{entry.entry_id}_group_a"
        )
        is None
    )
    assert scheduled == [entry.entry_id]


async def test_refused_group_delete_changes_nothing(hass, loaded, adapters):
    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    entry = loaded
    adapters[0].response = {"status": "error", "error": "device did not respond"}
    with pytest.raises(HomeAssistantError) as caught:
        await async_remove_config_entry_device(
            hass, entry, _device(hass, entry, "group_a")
        )
    assert "rejected" in str(caught.value)
    assert entry.data == valid_config()
    assert entry.options["pending_group_operation"]["action"] == "remove"


async def test_free_circuit_delete_drops_its_relay_channel(hass, loaded, monkeypatch):
    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    entry = loaded
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _id: None)
    data = valid_config()
    data["groups"], data["rooms"] = [], []
    hass.config_entries.async_update_entry(entry, data=data)
    assert await async_remove_config_entry_device(
        hass, entry, _device(hass, entry, "light_b")
    )
    assert [item["id"] for item in entry.data["lights"]] == ["light_a"]
    assert [item["id"] for item in entry.data["relay_channels"]] == ["relay_a"]


async def test_stale_devices_are_removed_on_setup(
    hass, mqtt_mock, adapters, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_light_gone")},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(stale.id) is None
    assert _device(hass, entry, "light_a") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


def _switch(endpoint, key):
    feature = {
        "name": "state",
        "property": key,
        "access": 7,
        "value_on": "ON",
        "value_off": "OFF",
    }
    return {"type": "switch", "endpoint": endpoint, "features": [feature]}


def _led(ieee, name):
    feature = {"name": "state", "property": "state", "access": 7}
    feature.update(value_on="ON", value_off="OFF")
    return {
        "ieee_address": ieee,
        "friendly_name": name,
        "endpoints": {"1": {}},
        "definition": {"exposes": [{"type": "light", "features": [feature]}]},
    }


def catalog():
    """The shared synthetic catalog plus one free relay channel and controller."""
    from custom_components.zigbee_led_orchestrator.topology import Topology

    result = Topology("zigbee2mqtt")
    result.update_devices(
        [
            {
                "ieee_address": "0x0000000000000001",
                "friendly_name": "relay",
                "endpoints": {"1": {"name": "l1"}, "2": {"name": "l2"}},
                "definition": {
                    "exposes": [_switch("l1", "state_l1"), _switch("l2", "state_l2")]
                },
            },
            _led("0x0000000000000002", "led1"),
            _led("0x0000000000000003", "led2"),
            {
                "ieee_address": "0x0000000000000004",
                "friendly_name": "relay2",
                "endpoints": {"1": {"name": "l3"}},
                "definition": {"exposes": [_switch("l3", "state_l3")]},
            },
            _led("0x0000000000000005", "led3"),
        ]
    )
    result.update_groups([GROUP])
    return result


async def _options(hass, entry, monkeypatch, step, choice):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    async def discover(hass, base_topic):
        return catalog()

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _id: None)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    picker = await getattr(flow, f"async_step_{step}")()
    assert picker["type"] == "form"
    return flow, await getattr(flow, f"async_step_{step}")(choice)


async def test_edit_circuit_keeps_ids_when_relay_mapping_changes(
    hass, loaded, monkeypatch
):
    entry = loaded
    flow, form = await _options(
        hass, entry, monkeypatch, "edit_circuit", {"circuit": "light_a"}
    )
    assert form["step_id"] == "circuit_details"
    defaults = {str(key): key.default() for key in form["data_schema"].schema}
    assert defaults == {
        "name": "Light A",
        "light": "zigbee2mqtt/led1 — 0x0000000000000002 / 1",
        "relay": "zigbee2mqtt/relay — 0x0000000000000001 / 1 / state_l1",
    }
    moved = await flow.async_step_circuit_details(
        {
            "name": "Kuchnia",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000004|1|state_l3",
        }
    )
    assert moved["type"] == "create_entry"
    assert entry.data["relay_channels"][0]["state_key"] == "state_l3"
    renamed = await flow.async_step_circuit_details(
        {
            "name": "Kuchnia",
            "light": "0x0000000000000002|1",
            "relay": "0x0000000000000001|1|state_l1",
        }
    )
    assert renamed["type"] == "create_entry"
    assert entry.data["lights"][0]["id"] == "light_a"
    assert entry.data["lights"][0]["name"] == "Kuchnia"
    assert entry.data["relay_channels"] == valid_config()["relay_channels"]


async def test_edit_free_circuit_moves_it_to_another_channel(hass, loaded, monkeypatch):
    entry = loaded
    data = valid_config()
    data["groups"], data["rooms"] = [], []
    hass.config_entries.async_update_entry(entry, data=data)
    flow, _form = await _options(
        hass, entry, monkeypatch, "edit_circuit", {"circuit": "light_b"}
    )
    taken = await flow.async_step_circuit_details(
        {
            "name": "Light B",
            "light": "0x0000000000000003|1",
            "relay": "0x0000000000000001|1|state_l1",
        }
    )
    assert taken["errors"] == {"base": "already_mapped"}
    result = await flow.async_step_circuit_details(
        {
            "name": "Light B",
            "light": "0x0000000000000005|1",
            "relay": "0x0000000000000004|1|state_l3",
        }
    )
    assert result["type"] == "create_entry"
    light = entry.data["lights"][1]
    assert (light["id"], light["device_ieee"], light["power_channel"]) == (
        "light_b",
        "0x0000000000000005",
        "relay_b",
    )
    assert entry.data["relay_channels"][1] == {
        "id": "relay_b",
        "device_ieee": "0x0000000000000004",
        "endpoint": 1,
        "state_key": "state_l3",
    }


async def test_edit_group_renames_it_in_zigbee2mqtt(
    hass, loaded, adapters, monkeypatch
):
    entry = loaded
    flow, form = await _options(
        hass, entry, monkeypatch, "edit_group", {"group": "group_a"}
    )
    assert form["step_id"] == "group_details"
    one = await flow.async_step_group_details({"name": "Salon", "members": []})
    assert one["errors"] == {"base": "need_two_members"}
    assert not adapters[0].requests
    result = await flow.async_step_group_details(
        {"name": "Salon", "members": ["light_a", "light_b"]}
    )
    assert result["type"] == "create_entry"
    assert adapters[0].requests == [
        ("group/rename", {"from": "group_a_orchestrator", "to": "Salon_orchestrator"})
    ]
    group = entry.data["groups"][0]
    assert (group["id"], group["z2m_id"], group["name"]) == ("group_a", 1, "Salon")


async def test_failed_group_rename_keeps_the_entry(hass, loaded, adapters, monkeypatch):
    entry = loaded
    adapters[0].response = {"status": "error", "error": "busy"}
    flow, _form = await _options(
        hass, entry, monkeypatch, "edit_group", {"group": "group_a"}
    )
    result = await flow.async_step_group_details(
        {"name": "Salon", "members": ["light_a", "light_b"]}
    )
    assert result["errors"] == {"base": "group_operation_failed"}
    assert entry.data == valid_config()


async def test_pending_group_operation_prevents_automatic_recreation():
    import asyncio

    from custom_components.zigbee_led_orchestrator.configuration import validate_config
    from custom_components.zigbee_led_orchestrator.models import (
        BridgeObservation,
        EvidenceQuality,
    )
    from custom_components.zigbee_led_orchestrator.runtime import Runtime

    adapter = BridgeAdapter()
    adapter.topology.update_groups([])
    instance = Runtime(
        validate_config(valid_config()), adapter, management_pending="group_a"
    )
    await instance.start()
    try:
        adapter.observation(
            BridgeObservation(True, 1, False, EvidenceQuality.UNKNOWN_SOURCE)
        )
        await asyncio.sleep(0)
        assert adapter.requests == []
    finally:
        await instance.close()
