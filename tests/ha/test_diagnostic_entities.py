"""Diagnostic entities separate request, phase and confirmation."""

# ruff: noqa: F811

from homeassistant.helpers import entity_registry as er

from tests.ha.test_init import mocked_adapter  # noqa: F401
from tests.ha.test_services import setup  # noqa: F401


def _state(hass, entry, unique_suffix):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        None, "zigbee_led_orchestrator", f"{entry.entry_id}_{unique_suffix}"
    )
    if entity_id is None:
        for platform in ("sensor", "binary_sensor"):
            entity_id = registry.async_get_entity_id(
                platform, "zigbee_led_orchestrator", f"{entry.entry_id}_{unique_suffix}"
            )
            if entity_id:
                break
    assert entity_id is not None
    return hass.states.get(entity_id)


async def test_diagnostic_entities_report_without_control(setup):
    hass, entry, _runtime, _adapter = setup
    registry = er.async_get(hass)
    for suffix in ("light_a_phase", "light_a_effective_state", "light_a_confirmed"):
        state = _state(hass, entry, suffix)
        assert state is not None
        assert state.state != "unavailable"
    confirmed = _state(hass, entry, "light_a_confirmed")
    # Bridge online arms control, so a trusted OFF report confirms the off state.
    assert confirmed.state == "on"
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    diagnostic = [e for e in entries if e.unique_id.endswith(("phase", "confirmed"))]
    assert diagnostic
    assert all(e.entity_category is not None for e in diagnostic)


async def test_circuits_and_group_are_separate_devices_with_their_entities(setup):
    hass, entry, _runtime, _adapter = setup
    registry = er.async_get(hass)
    entries = {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    a = entries[f"{entry.entry_id}_light_a"].device_id
    b = entries[f"{entry.entry_id}_light_b"].device_id
    group = entries[f"{entry.entry_id}_group_a"].device_id
    assert a and b and group and len({a, b, group}) == 3
    for suffix in ("phase", "effective_state", "confirmed"):
        assert entries[f"{entry.entry_id}_light_a_{suffix}"].device_id == a
        assert entries[f"{entry.entry_id}_light_b_{suffix}"].device_id == b


async def test_removing_group_member_is_rejected(setup):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, _runtime, _adapter = setup
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_light_a"), entry.entry_id
    )
    with pytest.raises(HomeAssistantError, match="group_a"):
        await async_remove_config_entry_device(hass, entry, device)
    assert any(item["id"] == "light_a" for item in entry.data["lights"])


async def test_remove_ungrouped_circuit_removes_its_entities_and_reserves_id(
    setup,
    monkeypatch,
):
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, _runtime, _adapter = setup
    data = dict(entry.data)
    data["groups"] = []
    hass.config_entries.async_update_entry(entry, data=data)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_light_a"), entry.entry_id
    )
    assert await async_remove_config_entry_device(hass, entry, device)
    assert {item["id"] for item in entry.data["lights"]} == {"light_b"}
    assert "light_a" in entry.options["retired_ids"]
    assert not er.async_entries_for_device(er.async_get(hass), device.id)


async def test_remove_group_waits_for_z2m_and_keeps_circuits(setup, monkeypatch):
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, runtime, adapter = setup
    data = dict(entry.data)
    data["rooms"] = []
    hass.config_entries.async_update_entry(entry, data=data)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"), entry.entry_id
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    calls = []

    async def request(path, payload):
        calls.append((path, payload))
        adapter.topology.update_groups([])
        return {"status": "ok", "data": {"id": 1, "force": False}}

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    assert await async_remove_config_entry_device(hass, entry, device)
    assert calls == [("group/remove", {"id": 1})]
    assert entry.data["groups"] == []
    assert len(entry.data["lights"]) == 2
    assert not entry.options.get("pending_group_operation")
    assert not runtime.adapter.active


async def test_failed_group_removal_remains_pending_and_blocks_group(
    setup,
    monkeypatch,
):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )
    from custom_components.zigbee_led_orchestrator.models import IntentPatch

    hass, entry, runtime, adapter = setup
    data = dict(entry.data)
    data["rooms"] = []
    hass.config_entries.async_update_entry(entry, data=data)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"), entry.entry_id
    )

    async def reject(path, payload):
        return {"status": "error", "error": "device offline"}

    monkeypatch.setattr(adapter, "request_bridge", reject, raising=False)
    with pytest.raises(HomeAssistantError, match="rejected"):
        await async_remove_config_entry_device(hass, entry, device)
    assert entry.options["pending_group_operation"]["id"] == "group_a"
    assert entry.data["groups"]
    assert not runtime.group_snapshot("group_a").available
    with pytest.raises(RuntimeError):
        runtime.submit_group("group_a", IntentPatch(on=True), source="test")


async def test_group_removal_rejects_response_for_different_group(setup, monkeypatch):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, _runtime, adapter = setup
    data = dict(entry.data)
    data["rooms"] = []
    hass.config_entries.async_update_entry(entry, data=data)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"), entry.entry_id
    )

    async def wrong_response(path, payload):
        adapter.topology.update_groups([])
        return {"status": "ok", "data": {"id": 2, "force": False}}

    monkeypatch.setattr(adapter, "request_bridge", wrong_response, raising=False)
    with pytest.raises(HomeAssistantError):
        await async_remove_config_entry_device(hass, entry, device)
    assert entry.data["groups"]


async def test_edit_group_renames_z2m_and_preserves_group_id(setup, monkeypatch):
    from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow

    hass, entry, _runtime, adapter = setup
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    calls = []

    async def request(path, payload):
        calls.append((path, payload))
        adapter.topology.update_groups(
            [
                {
                    "id": 1,
                    "friendly_name": "Salon_orchestrator",
                    "members": [
                        {"ieee_address": "0x0000000000000002", "endpoint": 1},
                        {"ieee_address": "0x0000000000000003", "endpoint": 1},
                    ],
                }
            ]
        )
        return {
            "status": "ok",
            "data": {"from": "group_a_orchestrator", "to": "Salon_orchestrator"},
        }

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    await flow.async_step_edit_group({"group": "group_a"})
    result = await flow.async_step_edit_group_details(
        {"name": "Salon", "members": ["light_a", "light_b"]}
    )
    assert result["type"] == "create_entry"
    assert entry.data["groups"][0]["id"] == "group_a"
    assert entry.data["groups"][0]["z2m_id"] == 1
    assert entry.data["groups"][0]["name"] == "Salon"
    assert calls == [
        ("group/rename", {"from": "group_a_orchestrator", "to": "Salon_orchestrator"})
    ]


async def test_rejected_group_edit_can_be_retried(setup, monkeypatch):
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
    )

    hass, entry, _runtime, adapter = setup
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    count = 0

    async def request(path, payload):
        nonlocal count
        count += 1
        if count == 1:
            return {"status": "error", "error": "unavailable"}
        adapter.topology.update_groups(
            [
                {
                    "id": 1,
                    "friendly_name": "Salon_orchestrator",
                    "members": [
                        {"ieee_address": "0x0000000000000002", "endpoint": 1},
                        {"ieee_address": "0x0000000000000003", "endpoint": 1},
                    ],
                }
            ]
        )
        return {
            "status": "ok",
            "data": {"from": "group_a_orchestrator", "to": "Salon_orchestrator"},
        }

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    group = dict(entry.data["groups"][0])
    with pytest.raises(HomeAssistantError):
        await async_edit_group(hass, entry, group, "Salon", ["light_a", "light_b"])
    await async_edit_group(hass, entry, group, "Salon", ["light_a", "light_b"])
    assert count == 2
    assert entry.data["groups"][0]["name"] == "Salon"


async def test_unknown_group_edit_is_not_replayed(setup, monkeypatch):
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
    )

    hass, entry, runtime, adapter = setup
    calls = 0

    async def timeout(path, payload):
        nonlocal calls
        calls += 1
        raise TimeoutError

    monkeypatch.setattr(adapter, "request_bridge", timeout, raising=False)
    group = dict(entry.data["groups"][0])
    for _ in range(2):
        with pytest.raises(HomeAssistantError):
            await async_edit_group(hass, entry, group, "Salon", ["light_a", "light_b"])
    assert calls == 1
    assert entry.options["pending_group_operation"]["inflight"] == "rename"
    assert not runtime.group_snapshot("group_a").available


async def test_edit_group_changes_exact_membership(setup, monkeypatch):
    import copy
    import json
    from pathlib import Path

    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
    )

    hass, entry, runtime, adapter = setup
    data = copy.deepcopy(dict(entry.data))
    channel = copy.deepcopy(data["relay_channels"][0])
    channel.update(id="relay_c", endpoint=3, state_key="state_l3")
    light = copy.deepcopy(data["lights"][0])
    light.update(
        id="light_c",
        device_ieee="0x0000000000000004",
        power_channel="relay_c",
        name="Third",
    )
    data["relay_channels"].append(channel)
    data["lights"].append(light)
    hass.config_entries.async_update_entry(entry, data=data)
    fixture = json.loads(Path("tests/fixtures/z2m/pilot_synthetic.json").read_text())
    devices = fixture["devices"]
    devices[0]["endpoints"]["3"] = {"name": "l3"}
    switch = copy.deepcopy(devices[0]["definition"]["exposes"][0])
    switch["endpoint"] = "l3"
    switch["features"][0]["property"] = "state_l3"
    devices[0]["definition"]["exposes"].append(switch)
    third = copy.deepcopy(devices[1])
    third.update(ieee_address="0x0000000000000004", friendly_name="room/led3")
    devices.append(third)
    adapter.topology.update_devices(devices)
    adapter.topology.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                    {"ieee_address": "0x0000000000000003", "endpoint": 1},
                ],
            }
        ]
    )
    monkeypatch.setattr(
        runtime, "_powered_lights", lambda: frozenset({"light_a", "light_b", "light_c"})
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    requests = []
    current_members = {("0x0000000000000002", 1), ("0x0000000000000003", 1)}

    async def request(path, payload):
        requests.append((path, payload))
        if path.endswith("remove"):
            current_members.remove(("0x0000000000000002", 1))
        else:
            current_members.add(("0x0000000000000004", 1))
        adapter.topology.update_groups(
            [
                {
                    "id": 1,
                    "friendly_name": "group_a_orchestrator",
                    "members": [
                        {"ieee_address": ieee, "endpoint": endpoint}
                        for ieee, endpoint in current_members
                    ],
                }
            ]
        )
        return {"status": "ok", "data": {}}

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    await async_edit_group(
        hass, entry, data["groups"][0], "group_a", ["light_b", "light_c"]
    )
    assert entry.data["groups"][0]["members"] == ["light_b", "light_c"]
    assert [path for path, _ in requests] == [
        "group/members/remove",
        "group/members/add",
    ]
    assert requests[0][1]["skip_disable_reporting"] is True
    assert not entry.options.get("pending_group_operation")
