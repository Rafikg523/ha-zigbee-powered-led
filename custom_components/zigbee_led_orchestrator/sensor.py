"""Phase and effective-state diagnostics, separate from the requested state."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import RuntimeDiagnosticEntity, virtual_device
from .models import EffectiveState, Phase


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = entry.runtime_data
    entities = []
    for light in runtime.config.lights:
        device = virtual_device(
            entry.entry_id, light.id, light.name, "circuit", hass.config.language
        )
        entities.append(PhaseSensor(runtime, entry.entry_id, light.id, device))
        entities.append(EffectiveStateSensor(runtime, entry.entry_id, light.id, device))
    async_add_entities(entities)


class PhaseSensor(RuntimeDiagnosticEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, runtime, entry_id: str, light_id: str, device=None) -> None:
        super().__init__(runtime, entry_id, light_id, "phase", device)
        self._attr_options = [item.value for item in Phase]

    @property
    def native_value(self) -> str:
        return self._runtime.snapshot(self._light_id).phase.value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"reason": self._runtime.snapshot(self._light_id).reason}


class EffectiveStateSensor(RuntimeDiagnosticEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, runtime, entry_id: str, light_id: str, device=None) -> None:
        super().__init__(runtime, entry_id, light_id, "effective_state", device)
        self._attr_options = [item.value for item in EffectiveState]

    @property
    def native_value(self) -> str:
        return self._runtime.snapshot(self._light_id).effective.value
