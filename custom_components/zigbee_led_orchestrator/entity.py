"""Shared base for read-only diagnostic entities backed by the runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from .configuration import circuit_label
from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import Runtime

MANUFACTURER = "Zigbee LED Orchestrator"
_MODELS = {
    "en": {"circuit": "LED circuit", "group": "Circuit group"},
    "pl": {"circuit": "Obwód LED", "group": "Grupa obwodów"},
}


def device_identifier(entry_id: str, item_id: str) -> tuple[str, str]:
    """Registry identifier of the virtual device for one circuit or group."""
    return (DOMAIN, f"{entry_id}_{item_id}")


def virtual_device(
    entry_id: str, item_id: str, name: str, kind: str, language: str
) -> DeviceInfo:
    """One device per configured circuit or group, listed on the entry page."""
    code = "pl" if str(language).lower().startswith("pl") else "en"
    return DeviceInfo(
        identifiers={device_identifier(entry_id, item_id)},
        name=name,
        manufacturer=MANUFACTURER,
        model=_MODELS[code][kind],
    )


def _diagnostic_label(runtime: Runtime, light_id: str) -> str:
    config = getattr(runtime, "config", None)
    for light in getattr(config, "lights", ()):
        if getattr(light, "id", None) == light_id:
            stored = getattr(light, "name", None)
            if isinstance(stored, str) and stored:
                return stored
            break
    return circuit_label(light_id)


class RuntimeDiagnosticEntity:
    """Report known diagnostics even while control is unavailable."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        runtime: Runtime,
        entry_id: str,
        light_id: str,
        key: str,
        device_info: DeviceInfo | None = None,
    ):
        self._runtime = runtime
        self._light_id = light_id
        self._attr_unique_id = f"{entry_id}_{light_id}_{key}"
        if device_info is not None:
            self._attr_device_info = device_info
            self._attr_translation_key = key
            return
        label = _diagnostic_label(runtime, light_id)
        self._attr_name = f"{label} {key.replace('_', ' ')}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def changed(light_ids: frozenset[str]) -> None:
            if self._light_id in light_ids:
                self.async_write_ha_state()

        self.async_on_remove(self._runtime.subscribe(changed))
