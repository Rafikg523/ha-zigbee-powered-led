"""Home Assistant entry lifecycle for Zigbee LED Orchestrator."""

import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "sensor", "binary_sensor"]


async def async_remove_config_entry_device(hass, entry, device) -> bool:
    """Remove an ungrouped virtual circuit from this integration."""
    import copy

    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    from .models import ControlMode

    identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
    data = copy.deepcopy(dict(entry.data))
    light = next(
        (
            item
            for item in data["lights"]
            if f"{entry.entry_id}_{item['id']}" in identifiers
        ),
        None,
    )
    if light is None:
        group = next(
            (
                item
                for item in data.get("groups", [])
                if f"{entry.entry_id}_{item['id']}" in identifiers
            ),
            None,
        )
        if group is None:
            raise HomeAssistantError("Unknown orchestrator device")
        await _async_remove_group(hass, entry, group, device)
        return True
    if entry.options.get("pending_group_operation"):
        raise HomeAssistantError("Complete the pending group operation first")
    group = next(
        (item for item in data.get("groups", []) if light["id"] in item["members"]),
        None,
    )
    if group is not None:
        raise HomeAssistantError(
            f"Circuit belongs to group {group.get('name', group['id'])}"
        )
    data["lights"] = [item for item in data["lights"] if item["id"] != light["id"]]
    data["relay_channels"] = [
        item for item in data["relay_channels"] if item["id"] != light["power_channel"]
    ]
    entry.runtime_data.set_mode(ControlMode.OBSERVE)
    options = dict(entry.options)
    options["retired_ids"] = [
        *options.get("retired_ids", []),
        light["id"],
        light["power_channel"],
    ]
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_device(registry, device.id):
        if entity.config_entry_id == entry.entry_id:
            registry.async_remove(entity.entity_id)
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def _async_remove_group(hass, entry, group, device) -> None:
    """Serialize group management through the runtime."""
    async with entry.runtime_data._management_lock:
        await _async_remove_group_locked(hass, entry, group, device)


async def _async_remove_group_locked(hass, entry, group, device) -> None:
    """Remove an owned Zigbee group after its catalog confirms deletion."""
    import asyncio
    import copy

    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    from .group_provisioner import z2m_group_name
    from .models import ControlMode

    runtime = entry.runtime_data
    adapter = runtime.adapter
    pending = entry.options.get("pending_group_operation")
    operation = {
        "action": "remove",
        "id": group["id"],
        "z2m_id": group.get("z2m_id"),
        "name": z2m_group_name(group.get("name", group["id"])),
    }
    if pending and any(pending.get(key) != value for key, value in operation.items()):
        raise HomeAssistantError("Another group operation needs to be completed first")
    if any(group["id"] in room["groups"] for room in entry.data.get("rooms", [])):
        raise HomeAssistantError("Remove the group from its room first")
    if (
        not getattr(adapter.topology, "groups_seen", False)
        or runtime._observations.bridge_online is not True
    ):
        raise HomeAssistantError("Zigbee2MQTT group catalog is unavailable")
    found = adapter.topology.group_named(operation["name"])
    lights = {item["id"]: item for item in entry.data["lights"]}
    expected = frozenset(
        (lights[item]["device_ieee"].lower(), lights[item]["endpoint"])
        for item in group["members"]
    )
    if found is not None and not found[1].members <= expected:
        raise HomeAssistantError("Zigbee2MQTT group contains unexpected members")
    if not pending:
        missing = found is None and operation["z2m_id"] not in dict(
            adapter.topology.groups()
        )
        if not missing and (
            operation["z2m_id"] is None
            or found is None
            or found[0] != operation["z2m_id"]
        ):
            raise HomeAssistantError("Cannot verify the Zigbee2MQTT group identity")
        if found is not None and found[1].members != expected:
            raise HomeAssistantError("Zigbee2MQTT group membership does not match")
        options = dict(entry.options)
        options["pending_group_operation"] = operation
        if missing and operation["z2m_id"] is None:
            pending = {**operation, "confirmed": True}
            options["pending_group_operation"] = pending
        hass.config_entries.async_update_entry(entry, options=options)
    runtime._management_pending = group["id"]
    runtime.set_mode(ControlMode.OBSERVE)
    if runtime._provision_task is not None and not runtime._provision_task.done():
        runtime._provision_task.cancel()
        await asyncio.gather(runtime._provision_task, return_exceptions=True)
    event = asyncio.Event()
    unsubscribe = adapter.subscribe_topology(lambda _version: event.set())

    def removed():
        return (
            not runtime._closed
            and adapter.topology.groups_seen
            and runtime._observations.bridge_online is True
            and operation["z2m_id"] not in dict(adapter.topology.groups())
        )

    try:
        found = adapter.topology.group_named(operation["name"])
        groups = dict(adapter.topology.groups())
        if found is not None and found[0] != operation["z2m_id"]:
            raise HomeAssistantError("Zigbee2MQTT group identity changed")
        if operation["z2m_id"] in groups and found is None:
            raise HomeAssistantError("Zigbee2MQTT group identity changed")
        if found is None and not (pending and pending.get("confirmed")):
            raise HomeAssistantError(
                "Group is absent but Zigbee2MQTT did not confirm removal; manual reconciliation is required"
            )
        if found is not None:
            try:
                async with asyncio.timeout(10):
                    response = await adapter.request_bridge(
                        "group/remove", {"id": operation["z2m_id"]}
                    )
                    if not isinstance(response, dict) or response.get("status") != "ok":
                        raise HomeAssistantError(
                            "Zigbee2MQTT rejected group removal; retry when all members are available"
                        )
                    result = response.get("data")
                    if (
                        not isinstance(result, dict)
                        or result.get("id")
                        not in (
                            operation["z2m_id"],
                            str(operation["z2m_id"]),
                            operation["name"],
                        )
                        or result.get("force") is not False
                    ):
                        raise HomeAssistantError(
                            "Zigbee2MQTT returned an invalid group removal response"
                        )
                    options = dict(entry.options)
                    options["pending_group_operation"] = {
                        **operation,
                        "confirmed": True,
                    }
                    hass.config_entries.async_update_entry(entry, options=options)
                    while not removed():
                        event.clear()
                        if removed():
                            break
                        await event.wait()
            except (TimeoutError, RuntimeError) as exc:
                raise HomeAssistantError(
                    "Group removal is unconfirmed; retry when Zigbee2MQTT is available"
                ) from exc
    finally:
        unsubscribe()
    if not removed():
        raise HomeAssistantError("Group removal is not confirmed by a current catalog")
    data = copy.deepcopy(dict(entry.data))
    data["groups"] = [item for item in data["groups"] if item["id"] != group["id"]]
    options = dict(entry.options)
    options.pop("pending_group_operation", None)
    options["retired_ids"] = [*options.get("retired_ids", []), group["id"]]
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_device(registry, device.id):
        if entity.config_entry_id == entry.entry_id:
            registry.async_remove(entity.entity_id)
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_setup_entry(hass, entry) -> bool:
    """Start one instance and attach its platforms.

    Control turns on by itself when the bridge and catalog are ready.
    """
    from .configuration import validate_config
    from .group_provisioner import stored_group_ids
    from .mqtt_adapter import MqttAdapter
    from .preferences import PreferencesStore, load_preferences
    from .runtime import Runtime
    from .services import async_register_services

    if hass.data.get(DOMAIN) is not None:
        return False
    config = validate_config(entry.data)
    # Reserve synchronously before the first await to prevent competing writers.
    hass.data[DOMAIN] = entry.entry_id
    runtime = Runtime(
        config,
        MqttAdapter(hass, config),
        user_inactive=entry.options.get("active") is False,
        management_pending=(entry.options.get("pending_group_operation") or {}).get(
            "id"
        ),
        management_members=tuple(
            (entry.options.get("pending_group_operation") or {}).get("members", ())
        ),
    )
    store = PreferencesStore(hass, entry.entry_id)
    try:
        try:
            raw = await store.async_load()
        except Exception as exc:  # noqa: BLE001 - unreadable storage means no preferences
            raw, runtime.preferences_error = None, f"storage unreadable: {exc}"
        preferences, error = load_preferences(raw, config)
        if error is not None:
            runtime.preferences_error = error
            _LOGGER.warning("Ignoring stored preferences: %s", error)
        runtime.restore_preferences(preferences)
        runtime.set_preferences_listener(
            lambda: store.schedule_save(runtime.preferences)
        )

        def remember_group(group_id: str, z2m_id: int) -> None:
            updated = stored_group_ids(entry.data.get("groups"), group_id, z2m_id)
            if updated is None:
                return
            data = dict(entry.data)
            data["groups"] = updated
            hass.config_entries.async_update_entry(entry, data=data)

        runtime.set_group_id_listener(remember_group)
        runtime.set_language(getattr(hass.config, "language", "en"))

        def publish_notice(key: str, message: str | None) -> None:
            from homeassistant.components import persistent_notification

            notification_id = f"{DOMAIN}_{entry.entry_id}_{key}"
            if message is None:
                persistent_notification.async_dismiss(hass, notification_id)
                return
            persistent_notification.async_create(
                hass, message, "Zigbee LED Orchestrator", notification_id
            )

        runtime.set_notice_listener(publish_notice)
        await runtime.start()
        entry.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if not hass.services.has_service(DOMAIN, "pause"):
            async_register_services(hass)
        hass.data[f"{DOMAIN}_store"] = store
    except BaseException:
        await runtime.close()
        hass.data.pop(DOMAIN, None)
        raise
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload the platforms and release every subscription and worker."""
    from .services import async_remove_services

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    runtime.set_preferences_listener(None)
    runtime.set_notice_listener(None)
    from homeassistant.components import persistent_notification

    for key in tuple(getattr(runtime, "notices", ())):
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_{entry.entry_id}_{key}")
    store = hass.data.pop(f"{DOMAIN}_store", None)
    if store is not None:
        await store.async_save(runtime.preferences())
    async_remove_services(hass)
    await runtime.close()
    if hass.data.get(DOMAIN) == entry.entry_id:
        hass.data.pop(DOMAIN, None)
    return True
