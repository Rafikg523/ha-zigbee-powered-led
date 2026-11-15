"""Explicit, recoverable changes to an orchestrator-owned Zigbee group."""

from __future__ import annotations

import asyncio
import copy

from homeassistant.exceptions import HomeAssistantError

from .configuration import validate_config
from .group_provisioner import z2m_group_name
from .models import ControlMode
from .topology import validate_topology


def _save(hass, entry, operation):
    options = dict(entry.options)
    options["pending_group_operation"] = copy.deepcopy(operation)
    hass.config_entries.async_update_entry(entry, options=options)


async def async_edit_group(hass, entry, group, name, members, *, mapping=None):
    """Change a group's name and members while stopping competing operations."""
    runtime = entry.runtime_data
    async with runtime._management_lock:
        await _edit_locked(hass, entry, group, name, members, mapping=mapping)


async def _edit_locked(
    hass, entry, group, name, members, *, mapping=None, reconcile=False
):
    runtime = entry.runtime_data
    adapter = runtime.adapter
    if group not in entry.data.get("groups", []):
        raise HomeAssistantError("Group configuration changed; reopen the editor")
    original_name = z2m_group_name(group.get("name", group["id"]))
    new_name = z2m_group_name(name)
    target_members = [
        item["id"] for item in entry.data["lights"] if item["id"] in members
    ]
    operation = {
        "action": "edit",
        "id": group["id"],
        "z2m_id": group.get("z2m_id"),
        "name": original_name,
        "new_name": new_name,
        "members": target_members,
    }
    data = copy.deepcopy(dict(entry.data))
    if mapping is not None:
        # Keep logical IDs while changing only the selected circuit and channel.
        light, channel = mapping["light"], mapping["channel"]
        if (
            light["id"] not in group["members"]
            or light["power_channel"] != channel["id"]
        ):
            raise HomeAssistantError("Invalid circuit mapping")
        previous = next(item for item in data["lights"] if item["id"] == light["id"])
        if previous["power_channel"] != channel["id"]:
            raise HomeAssistantError("Circuit channel identity changed")
        data["lights"] = [
            copy.deepcopy(light) if item["id"] == light["id"] else item
            for item in data["lights"]
        ]
        data["relay_channels"] = [
            copy.deepcopy(channel) if item["id"] == channel["id"] else item
            for item in data["relay_channels"]
        ]
        operation["mapping"] = copy.deepcopy(mapping)
    saved_group = next(item for item in data["groups"] if item["id"] == group["id"])
    saved_group.update(name=name, members=target_members)
    validate_topology(validate_config(data), adapter.topology, require_groups=False)
    pending = entry.options.get("pending_group_operation")
    if pending and any(pending.get(key) != value for key, value in operation.items()):
        raise HomeAssistantError("Another group operation needs to be completed first")
    if (
        runtime._closed
        or not adapter.topology.groups_seen
        or runtime._observations.bridge_online is not True
    ):
        raise HomeAssistantError("Zigbee2MQTT catalog is unavailable")
    lights = {item["id"]: item for item in entry.data["lights"]}
    old_endpoints = {
        item: (light["device_ieee"].lower(), light["endpoint"])
        for item, light in lights.items()
    }
    endpoints = {
        item["id"]: (item["device_ieee"].lower(), item["endpoint"])
        for item in data["lights"]
    }
    old = {old_endpoints[item] for item in group["members"]}
    desired = {endpoints[item] for item in target_members}
    found_group = adapter.topology.group_named(original_name) or (
        adapter.topology.group_named(new_name)
    )
    if (
        operation["z2m_id"] is not None
        and found_group is not None
        and found_group[0] != operation["z2m_id"]
    ):
        raise HomeAssistantError("Zigbee2MQTT group identity changed")
    if operation["z2m_id"] is None and found_group is not None:
        operation["z2m_id"] = found_group[0]
    current = dict(adapter.topology.groups()).get(operation["z2m_id"])
    if current is None and found_group is not None:
        current = found_group[1]
        operation["z2m_id"] = found_group[0]
    if current is None or current.name not in (original_name, new_name):
        raise HomeAssistantError("Cannot verify the Zigbee2MQTT group identity")
    if not current.members <= old | desired:
        raise HomeAssistantError("Group contains unexpected members")
    if not pending and current.name != original_name:
        raise HomeAssistantError(
            "Zigbee2MQTT group does not match the saved configuration"
        )
    if new_name != original_name:
        named = adapter.topology.group_named(new_name)
        if named is not None and named[0] != operation["z2m_id"]:
            raise HomeAssistantError("Group name is already in use")
    if pending and pending.get("inflight") and not reconcile:
        raise HomeAssistantError(
            "Previous group request has an unknown result and needs reconciliation"
        )
    completed = list(pending.get("completed", [])) if pending else []
    operation["completed"] = completed
    if pending and pending.get("inflight"):
        operation["inflight"] = pending["inflight"]
    _save(hass, entry, operation)
    runtime._management_pending = group["id"]
    runtime._management_extra_members = set(target_members)
    runtime.set_mode(ControlMode.OBSERVE)
    if runtime._provision_task is not None and not runtime._provision_task.done():
        runtime._provision_task.cancel()
        await asyncio.gather(runtime._provision_task, return_exceptions=True)
    event = asyncio.Event()
    unsubscribe = adapter.subscribe_topology(lambda _version: event.set())

    def catalog():
        if (
            runtime._closed
            or not adapter.topology.groups_seen
            or runtime._observations.bridge_online is not True
        ):
            return None
        return dict(adapter.topology.groups()).get(operation["z2m_id"])

    async def step(key, path, payload, done):
        current_group = catalog()
        if current_group is None:
            raise HomeAssistantError("Group disappeared from the Zigbee2MQTT catalog")
        valid_names = (
            {original_name, new_name} if path == "group/rename" else {new_name}
        )
        if (
            current_group.name not in valid_names
            or not current_group.members <= old | desired
        ):
            raise HomeAssistantError("Zigbee2MQTT group identity or membership changed")
        if reconcile and operation.get("inflight") == key:
            # The operator checked Z2M has finished requests; adopt an applied
            # step without sending it again, or explicitly retry an unapplied one.
            if done(current_group):
                completed.append(key)
            operation.pop("inflight", None)
            _save(hass, entry, operation)
        if key not in completed:
            if done(current_group):
                raise HomeAssistantError("Unconfirmed external group change")
            operation["inflight"] = key
            _save(hass, entry, operation)
            try:
                async with asyncio.timeout(10):
                    response = await adapter.request_bridge(path, payload)
            except (TimeoutError, RuntimeError) as exc:
                raise HomeAssistantError("Group request outcome is unknown") from exc
            if not isinstance(response, dict) or response.get("status") != "ok":
                if catalog() == current_group:
                    operation.pop("inflight", None)
                    _save(hass, entry, operation)
                raise HomeAssistantError("Zigbee2MQTT rejected the group request")
            if path == "group/rename":
                result = response.get("data")
                if (
                    not isinstance(result, dict)
                    or result.get("from") != original_name
                    or result.get("to") != new_name
                ):
                    raise HomeAssistantError("Unexpected group rename response")
            completed.append(key)
            operation.pop("inflight", None)
            _save(hass, entry, operation)
        try:
            async with asyncio.timeout(10):
                while True:
                    item = catalog()
                    if item is not None and done(item):
                        break
                    event.clear()
                    item = catalog()
                    if item is not None and done(item):
                        break
                    await event.wait()
        except TimeoutError as exc:
            raise HomeAssistantError(
                "Group change is not confirmed by the catalog"
            ) from exc

    try:
        if original_name != new_name:
            await step(
                "rename",
                "group/rename",
                {"from": original_name, "to": new_name},
                lambda item: item.name == new_name,
            )
        for item_id in group["members"]:
            endpoint = old_endpoints[item_id]
            if endpoint in desired:
                continue
            key = "remove:" + item_id
            if key in completed:
                continue
            current_g = catalog()
            if current_g is not None and endpoint not in current_g.members:
                completed.append(key)
                continue
            try:
                device = adapter.topology.resolve_device(endpoint[0])
                dev_name = device.name
            except KeyError:
                dev_name = endpoint[0]
            await step(
                key,
                "group/members/remove",
                {
                    "group": new_name,
                    "device": dev_name,
                    "endpoint": endpoint[1],
                    "skip_disable_reporting": True,
                },
                lambda item, ep=endpoint: ep not in item.members,
            )
        for item_id in target_members:
            endpoint = endpoints[item_id]
            current_g = catalog()
            if current_g is not None and endpoint in current_g.members:
                continue
            key = "add:" + item_id
            if key in completed:
                continue
            try:
                device = adapter.topology.resolve_device(endpoint[0])
                dev_name = device.name
            except KeyError:
                dev_name = endpoint[0]
            await step(
                key,
                "group/members/add",
                {"group": new_name, "device": dev_name, "endpoint": endpoint[1]},
                lambda item, ep=endpoint: ep in item.members,
            )
        current = catalog()
        if current is None or current.name != new_name or current.members != desired:
            raise HomeAssistantError("Final group catalog does not match")
        validate_topology(validate_config(data), adapter.topology, require_groups=False)
        options = dict(entry.options)
        options.pop("pending_group_operation", None)
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        hass.config_entries.async_schedule_reload(entry.entry_id)
    finally:
        unsubscribe()


async def async_recover_group(hass, entry, *, confirmed=False):
    """Resume only after the operator has settled outstanding Z2M requests."""
    if confirmed is not True:
        raise HomeAssistantError(
            "Confirm that Zigbee2MQTT has finished pending requests"
        )
    runtime = entry.runtime_data
    async with runtime._management_lock:
        pending = entry.options.get("pending_group_operation")
        if not isinstance(pending, dict):
            raise HomeAssistantError("No pending group operation")
        group = next(
            (item for item in entry.data["groups"] if item["id"] == pending.get("id")),
            None,
        )
        if group is None:
            raise HomeAssistantError("Group configuration changed")
        if pending["action"] == "edit":
            from .group_provisioner import normalize_group_name

            await _edit_locked(
                hass,
                entry,
                group,
                normalize_group_name(pending["new_name"]),
                pending["members"],
                mapping=pending.get("mapping"),
                reconcile=True,
            )
        elif pending["action"] == "remove":
            from homeassistant.helpers import device_registry as dr

            from . import _async_remove_group_locked
            from .const import DOMAIN

            if (
                not runtime.adapter.topology.groups_seen
                or runtime._observations.bridge_online is not True
            ):
                raise HomeAssistantError("Zigbee2MQTT catalog is unavailable")
            if pending["z2m_id"] not in dict(runtime.adapter.topology.groups()):
                pending = {**pending, "confirmed": True}
                _save(hass, entry, pending)
            device = dr.async_get(hass).async_get_device_by_identifier(
                (DOMAIN, f"{entry.entry_id}_{group['id']}"),
                entry.entry_id,
            )
            if device is None:
                raise HomeAssistantError("Group device is unavailable")
            await _async_remove_group_locked(hass, entry, group, device)
            registry = dr.async_get(hass)
            if registry.async_get(device.id):
                registry.async_remove_device(device.id)
        else:
            raise HomeAssistantError("Unknown group operation")
