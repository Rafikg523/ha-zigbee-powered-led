"""Confirmation diagnostics: true only with current device evidence."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import RuntimeDiagnosticEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        ConfirmedSensor(runtime, entry.entry_id, light.id)
        for light in runtime.config.lights
    )


class ConfirmedSensor(RuntimeDiagnosticEntity, BinarySensorEntity):
    def __init__(self, runtime, entry_id: str, light_id: str) -> None:
        super().__init__(runtime, entry_id, light_id, "confirmed")

    @property
    def is_on(self) -> bool:
        return self._runtime.snapshot(self._light_id).confirmed
