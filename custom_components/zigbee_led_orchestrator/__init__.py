"""Home Assistant entry lifecycle for Zigbee LED Orchestrator."""

import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "sensor", "binary_sensor"]


async def async_setup_entry(hass, entry) -> bool:
    """Start one instance in observation mode and attach its platforms."""
    from .configuration import validate_config
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
    store = hass.data.pop(f"{DOMAIN}_store", None)
    if store is not None:
        await store.async_save(runtime.preferences())
    async_remove_services(hass)
    await runtime.close()
    if hass.data.get(DOMAIN) == entry.entry_id:
        hass.data.pop(DOMAIN, None)
    return True
