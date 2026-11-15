"""Management completes without publishing light or relay commands."""

# ruff: noqa: F811

import copy
import json
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zigbee_led_orchestrator.config_flow import OptionsFlow
from tests.ha.test_init import mocked_adapter  # noqa: F401
from tests.ha.test_services import setup  # noqa: F401


async def test_edit_does_not_adopt_a_different_native_group_id(setup):
    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
    )

    hass, entry, _runtime, adapter = setup
    data = copy.deepcopy(dict(entry.data))
    data["groups"][0]["z2m_id"] = 99
    hass.config_entries.async_update_entry(entry, data=data)
    with pytest.raises(HomeAssistantError, match="identity"):
        await async_edit_group(
            hass, entry, entry.data["groups"][0], "group_a", ["light_a", "light_b"]
        )
    assert not entry.options.get("pending_group_operation")
    assert not adapter.sent


def add_replacement(adapter):
    devices = json.loads(Path("tests/fixtures/z2m/pilot_synthetic.json").read_text())[
        "devices"
    ]
    replacement = copy.deepcopy(devices[1])
    replacement.update(
        ieee_address="0x0000000000000004", friendly_name="room/replacement"
    )
    devices.append(replacement)
    adapter.topology.update_devices(devices)
    publish_group(adapter, "group_a_orchestrator", {2, 3})
    return devices


def publish_group(adapter, name, members):
    adapter.topology.update_groups(
        [
            {
                "id": 1,
                "friendly_name": name,
                "members": [
                    {"ieee_address": f"0x{member:016x}", "endpoint": 1}
                    for member in sorted(members)
                ],
            }
        ]
    )


async def test_remap_group_member_keeps_entity_identity_and_exact_membership(
    setup, monkeypatch
):
    hass, entry, runtime, adapter = setup
    add_replacement(adapter)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)

    async def discover(hass, base_topic):
        return adapter.topology

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.config_flow.discover_topology",
        discover,
    )
    calls = []
    members = {2, 3}

    async def request(path, payload):
        calls.append((path, payload))
        if path == "group/members/remove":
            members.remove(2)
        elif path == "group/members/add":
            members.add(4)
        else:
            pytest.fail(f"Unexpected request: {path}")
        publish_group(adapter, "group_a_orchestrator", members)
        return {
            "status": "ok",
            "data": {
                "group": "group_a_orchestrator",
                "device": payload["device"],
                "endpoint": 1,
            },
        }

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    await flow.async_step_edit_circuit({"circuit": "light_a"})
    result = await flow.async_step_edit_circuit_details(
        {
            "name": "Replacement",
            "light": "0x0000000000000004|1",
            "relay": "0x0000000000000001|1|state_l1",
        }
    )
    assert result["type"] == "create_entry"
    assert entry.data["lights"][0]["id"] == "light_a"
    assert entry.data["lights"][0]["device_ieee"] == "0x0000000000000004"
    assert entry.data["groups"][0]["members"] == ["light_a", "light_b"]
    assert adapter.topology.resolve_group(1).members == frozenset(
        {("0x0000000000000003", 1), ("0x0000000000000004", 1)}
    )
    assert [path for path, _ in calls] == ["group/members/remove", "group/members/add"]
    assert not adapter.sent
    assert not entry.options.get("pending_group_operation")
    assert not runtime.snapshot("light_a").desired.on


async def test_explicit_recovery_accepts_applied_rename_without_republishing(
    setup, monkeypatch
):
    from custom_components.zigbee_led_orchestrator import group_management

    hass, entry, _runtime, adapter = setup
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    calls = []

    async def request(path, payload):
        calls.append(path)
        publish_group(adapter, "Salon_orchestrator", {2, 3})
        raise TimeoutError

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    with pytest.raises(HomeAssistantError):
        await group_management.async_edit_group(
            hass, entry, entry.data["groups"][0], "Salon", ["light_a", "light_b"]
        )
    # A restart persists JSON only, not tasks or an old intent.
    hass.config_entries.async_update_entry(
        entry, options=json.loads(json.dumps(dict(entry.options)))
    )
    await group_management.async_recover_group(hass, entry, confirmed=True)
    assert entry.data["groups"][0]["name"] == "Salon"
    assert not entry.options.get("pending_group_operation")
    assert calls == ["group/rename"]
    assert not adapter.sent


async def test_recovery_requires_operator_confirmation(setup):
    from custom_components.zigbee_led_orchestrator.group_management import (
        async_recover_group,
    )

    hass, entry, _runtime, adapter = setup
    with pytest.raises(HomeAssistantError):
        await async_recover_group(hass, entry, confirmed=False)
    assert not adapter.sent


async def test_recovery_is_available_in_options_only_when_pending(setup, monkeypatch):
    hass, entry, _runtime, _adapter = setup
    flow = OptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    menu = await flow.async_step_init()
    assert "recover_group" not in menu["menu_options"]
    options = dict(entry.options)
    options["pending_group_operation"] = {
        "action": "edit",
        "id": "group_a",
        "z2m_id": 1,
        "name": "group_a_orchestrator",
        "new_name": "Salon_orchestrator",
        "members": ["light_a", "light_b"],
        "inflight": "rename",
    }
    hass.config_entries.async_update_entry(entry, options=options)
    menu = await flow.async_step_init()
    assert "recover_group" in menu["menu_options"]
    result = await flow.async_step_recover_group({"confirmed": False})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "confirmation_required"


async def test_pending_remap_blocks_member_commands_even_if_control_rearms(setup):
    from custom_components.zigbee_led_orchestrator.models import (
        ControlMode,
        IntentPatch,
    )

    _hass, _entry, runtime, adapter = setup
    runtime._management_pending = "group_a"
    runtime.set_mode(ControlMode.ACTIVE)
    with pytest.raises(RuntimeError, match="management"):
        runtime.submit(("light_a",), IntentPatch(on=True), source="test")
    assert not runtime.snapshot("light_a").available
    assert not adapter.sent


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_partial_remap_recovers_only_missing_steps(
    setup, monkeypatch, lost_reply
):
    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
        async_recover_group,
    )

    hass, entry, _runtime, adapter = setup
    devices = add_replacement(adapter)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    light = {**entry.data["lights"][0], "device_ieee": "0x0000000000000004"}
    mapping = {"light": light, "channel": dict(entry.data["relay_channels"][0])}
    calls = []
    recovering = False

    async def request(path, payload):
        calls.append(path)
        if path.endswith("remove"):
            publish_group(adapter, "group_a_orchestrator", {3})
            if lost_reply:
                raise TimeoutError
        elif not recovering:
            return {"status": "error", "error": "controller offline"}
        else:
            publish_group(adapter, "group_a_orchestrator", {3, 4})
        return {
            "status": "ok",
            "data": {
                "group": "group_a_orchestrator",
                "device": payload["device"],
                "endpoint": 1,
            },
        }

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    with pytest.raises(HomeAssistantError):
        await async_edit_group(
            hass,
            entry,
            entry.data["groups"][0],
            "group_a",
            ["light_a", "light_b"],
            mapping=mapping,
        )
    assert entry.data["lights"][0]["device_ieee"] == "0x0000000000000002"
    assert (
        entry.options["pending_group_operation"]["mapping"]["light"]["device_ieee"]
        == "0x0000000000000004"
    )
    # Persisted journal can be resumed after a restart without an old ON intent.
    hass.config_entries.async_update_entry(
        entry, options=json.loads(json.dumps(dict(entry.options)))
    )
    adapter.topology.update_devices(
        [device for device in devices if device["ieee_address"] != "0x0000000000000002"]
    )
    recovering = True
    await async_recover_group(hass, entry, confirmed=True)
    assert entry.data["lights"][0]["device_ieee"] == "0x0000000000000004"
    assert calls.count("group/members/remove") == 1
    assert not entry.options.get("pending_group_operation")
    assert not adapter.sent


async def test_recovery_refuses_reused_group_identity(setup, monkeypatch):
    from custom_components.zigbee_led_orchestrator.group_management import (
        async_edit_group,
        async_recover_group,
    )

    hass, entry, _runtime, adapter = setup

    async def timeout(path, payload):
        raise TimeoutError

    monkeypatch.setattr(adapter, "request_bridge", timeout, raising=False)
    with pytest.raises(HomeAssistantError):
        await async_edit_group(
            hass, entry, entry.data["groups"][0], "Salon", ["light_a", "light_b"]
        )
    publish_group(adapter, "Other_owner", {2, 3})
    with pytest.raises(HomeAssistantError, match="identity"):
        await async_recover_group(hass, entry, confirmed=True)
    assert entry.data["groups"][0].get("name", "group_a") == "group_a"
    assert entry.options["pending_group_operation"]


async def test_group_delete_disconnect_does_not_count_as_confirmed_absence(
    setup, monkeypatch
):
    import asyncio

    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, _runtime, adapter = setup
    hass.config_entries.async_update_entry(entry, data={**entry.data, "rooms": []})
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"),
        entry.entry_id,
    )
    replied = asyncio.Event()

    async def request(path, payload):
        adapter.topology.update_groups([], catalog=False)
        replied.set()
        return {"status": "ok", "data": {"id": 1, "force": False}}

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    task = asyncio.create_task(async_remove_config_entry_device(hass, entry, device))
    try:
        await replied.wait()
        await asyncio.sleep(0)
        assert entry.data["groups"], (
            "A disconnected empty cache is not proof of deletion"
        )
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_explicit_recovery_finishes_delete_after_lost_reply(setup, monkeypatch):
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )
    from custom_components.zigbee_led_orchestrator.group_management import (
        async_recover_group,
    )

    hass, entry, _runtime, adapter = setup
    hass.config_entries.async_update_entry(entry, data={**entry.data, "rooms": []})
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"),
        entry.entry_id,
    )
    calls = []

    async def request(path, payload):
        calls.append(path)
        adapter.topology.update_groups([])
        raise TimeoutError

    monkeypatch.setattr(adapter, "request_bridge", request, raising=False)
    with pytest.raises(HomeAssistantError):
        await async_remove_config_entry_device(hass, entry, device)
    await async_recover_group(hass, entry, confirmed=True)
    assert not entry.data["groups"]
    assert dr.async_get(hass).async_get(device.id) is None
    assert calls == ["group/remove"]


async def test_group_not_yet_created_in_z2m_can_be_removed(setup, monkeypatch):
    from homeassistant.helpers import device_registry as dr

    from custom_components.zigbee_led_orchestrator import (
        async_remove_config_entry_device,
    )

    hass, entry, _runtime, adapter = setup
    data = copy.deepcopy(dict(entry.data))
    data["rooms"] = []
    data["groups"][0].pop("z2m_id")
    hass.config_entries.async_update_entry(entry, data=data)
    adapter.topology.update_groups([])
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", lambda _: None)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zigbee_led_orchestrator", f"{entry.entry_id}_group_a"),
        entry.entry_id,
    )
    assert await async_remove_config_entry_device(hass, entry, device)
    assert not entry.data["groups"]
    assert not adapter.sent
