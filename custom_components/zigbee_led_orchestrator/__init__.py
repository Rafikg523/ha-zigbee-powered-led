"""Home Assistant entry lifecycle for Zigbee LED Orchestrator."""

from .const import DOMAIN


async def async_setup_entry(hass, entry) -> bool:
    """Start one instance in observation mode and attach its light platform."""
    from .configuration import validate_config
    from .mqtt_adapter import MqttAdapter
    from .runtime import Runtime

    if hass.data.get(DOMAIN) is not None:
        return False
    config = validate_config(entry.data)
    # Reserve synchronously before the first await to prevent competing writers.
    hass.data[DOMAIN] = entry.entry_id
    runtime = Runtime(config, MqttAdapter(hass, config))
    try:
        await runtime.start()
        entry.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(entry, ["light"])
    except BaseException:
        await runtime.close()
        hass.data.pop(DOMAIN, None)
        raise
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload the platform and release every subscription and worker."""
    if not await hass.config_entries.async_unload_platforms(entry, ["light"]):
        return False
    await entry.runtime_data.close()
    if hass.data.get(DOMAIN) == entry.entry_id:
        hass.data.pop(DOMAIN, None)
    return True
