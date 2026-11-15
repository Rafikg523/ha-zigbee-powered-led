"""Home Assistant entry lifecycle for Zigbee LED Orchestrator."""

import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "sensor", "binary_sensor"]


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
    runtime = Runtime(config, MqttAdapter(hass, config))
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
        _remove_stale_devices(hass, entry, config)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if not hass.services.has_service(DOMAIN, "pause"):
            async_register_services(hass)
        hass.data[f"{DOMAIN}_store"] = store
    except BaseException:
        await runtime.close()
        hass.data.pop(DOMAIN, None)
        raise
    return True


def _configured_ids(entry, config) -> set[str]:
    from .entity import device_identifier

    ids = {light.id for light in config.lights}
    ids.update(group.id for group in config.groups)
    return {device_identifier(entry.entry_id, item)[1] for item in ids}


def _remove_stale_devices(hass, entry, config) -> None:
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    expected = _configured_ids(entry, config)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not any(
            domain == DOMAIN and value in expected
            for domain, value in device.identifiers
        ):
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


def _device_item(entry, device_entry) -> str | None:
    prefix = f"{entry.entry_id}_"
    for domain, value in device_entry.identifiers:
        if domain == DOMAIN and value.startswith(prefix):
            return value[len(prefix) :]
    return None


async def async_remove_config_entry_device(hass, entry, device_entry) -> bool:
    """Delete a circuit or group from the device list.

    A circuit that belongs to a group is refused. A group that exists in
    Zigbee2MQTT is removed there first; the entry keeps it until Zigbee2MQTT
    confirms, so a failure leaves both sides unchanged.
    """
    from homeassistant.exceptions import HomeAssistantError

    from .group_provisioner import z2m_group_name
    from .management import CircuitInGroupError, find, without_circuit, without_group

    item_id = _device_item(entry, device_entry)
    if item_id is None:
        return True
    if find(entry.data, "lights", item_id) is not None:
        try:
            data = without_circuit(entry.data, item_id)
        except CircuitInGroupError as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="circuit_in_group",
                translation_placeholders={"group": exc.group_name},
            ) from exc
    elif (group := find(entry.data, "groups", item_id)) is not None:
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="runtime_not_running"
            )
        name = group.get("name", item_id)
        try:
            if runtime.catalog_group(name) is not None:
                await runtime.change_group(
                    item_id, "group/remove", {"id": z2m_group_name(name)}
                )
        except TimeoutError as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="group_remove_unknown"
            ) from exc
        except Exception as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="group_remove_failed",
                translation_placeholders={"error": str(exc)},
            ) from exc
        data = without_group(entry.data, item_id)
    else:
        return True
    hass.config_entries.async_update_entry(entry, data=data)
    hass.config_entries.async_schedule_reload(entry.entry_id)
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
