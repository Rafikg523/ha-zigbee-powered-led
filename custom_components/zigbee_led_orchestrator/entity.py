"""Shared base for read-only diagnostic entities backed by the runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory

if TYPE_CHECKING:
    from .runtime import Runtime


class RuntimeDiagnosticEntity:
    """Report known diagnostics even while control is unavailable."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, runtime: Runtime, entry_id: str, light_id: str, key: str):
        self._runtime = runtime
        self._light_id = light_id
        self._attr_unique_id = f"{entry_id}_{light_id}_{key}"
        self._attr_name = f"{light_id} {key.replace('_', ' ')}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def changed(light_ids: frozenset[str]) -> None:
            if self._light_id in light_ids:
                self.async_write_ha_state()

        self.async_on_remove(self._runtime.subscribe(changed))
