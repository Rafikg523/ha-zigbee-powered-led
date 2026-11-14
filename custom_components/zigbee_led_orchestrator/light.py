"""Home Assistant light presentation over the orchestration runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.exceptions import HomeAssistantError

from .models import IntentPatch

if TYPE_CHECKING:
    from .runtime import Runtime


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create entities from the validated runtime configuration."""
    runtime = entry.runtime_data
    async_add_entities(
        OrchestratorLight(
            runtime, entry.entry_id, light.id, light.min_kelvin, light.max_kelvin
        )
        for light in runtime.config.lights
    )
    lights = {light.id: light for light in runtime.config.lights}
    async_add_entities(
        OrchestratorGroupLight(
            runtime,
            entry.entry_id,
            group.id,
            group.members,
            max(lights[item].min_kelvin for item in group.members),
            min(lights[item].max_kelvin for item in group.members),
        )
        for group in runtime.config.groups
    )


class OrchestratorLight(LightEntity):
    """Represent desired state separately from physical observations."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: Runtime,
        entry_id: str,
        light_id: str,
        min_kelvin: int,
        max_kelvin: int,
        *,
        supports_color_temp: bool = False,
        supports_brightness: bool = False,
    ) -> None:
        self._runtime = runtime
        self._supports_color_temp = supports_color_temp
        self._supports_brightness = supports_brightness or supports_color_temp
        mode = (
            ColorMode.COLOR_TEMP
            if supports_color_temp
            else (ColorMode.BRIGHTNESS if supports_brightness else ColorMode.ONOFF)
        )
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}
        self._light_id = light_id
        self._attr_unique_id = f"{entry_id}_{light_id}"
        self._config_min_kelvin = min_kelvin
        self._config_max_kelvin = max_kelvin
        self._attr_min_color_temp_kelvin = min_kelvin
        self._attr_max_color_temp_kelvin = max_kelvin

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def changed(light_ids: frozenset[str]) -> None:
            if self._light_id in light_ids:
                self._refresh_capabilities()
                self.async_write_ha_state()

        self.async_on_remove(self._runtime.subscribe(changed))
        self._refresh_capabilities()

    def _refresh_capabilities(self) -> None:
        get_capabilities = getattr(self._runtime, "light_capabilities", None)
        if get_capabilities is None:
            return
        capabilities = get_capabilities(self._light_id)
        self._supports_brightness = capabilities.brightness_range is not None
        self._supports_color_temp = (
            capabilities.mired_range is not None and self._supports_brightness
        )
        mode = (
            ColorMode.COLOR_TEMP
            if self._supports_color_temp
            else (
                ColorMode.BRIGHTNESS if self._supports_brightness else ColorMode.ONOFF
            )
        )
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}
        if self._supports_color_temp:
            minimum, maximum = capabilities.mired_range
            low = max(self._config_min_kelvin, round(1_000_000 / maximum))
            high = min(self._config_max_kelvin, round(1_000_000 / minimum))
            if low > high:
                self._supports_color_temp = False
                self._attr_color_mode = ColorMode.BRIGHTNESS
                self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            else:
                self._attr_min_color_temp_kelvin = low
                self._attr_max_color_temp_kelvin = high

    @property
    def is_on(self) -> bool:
        return self._runtime.snapshot(self._light_id).desired.on

    @property
    def available(self) -> bool:
        return self._runtime.snapshot(self._light_id).available

    @property
    def brightness(self) -> int | None:
        return self._runtime.snapshot(self._light_id).desired.brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._runtime.snapshot(self._light_id).desired.color_temp_kelvin

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self._runtime.snapshot(self._light_id)
        return {
            "phase": snapshot.phase.value,
            "effective_state": snapshot.effective.value,
            "confirmed": snapshot.confirmed,
        }

    def _submit(self, patch: IntentPatch) -> None:
        try:
            self._runtime.submit((self._light_id,), patch, source="home_assistant")
        except RuntimeError as exc:
            raise HomeAssistantError("Light control is unavailable") from exc

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if kelvin is not None and not self._supports_color_temp:
            raise HomeAssistantError("Color temperature is unsupported")
        if brightness not in (None, 0) and not self._supports_brightness:
            raise HomeAssistantError("Brightness is unsupported")
        if kelvin is not None:
            kelvin = max(
                self.min_color_temp_kelvin, min(self.max_color_temp_kelvin, kelvin)
            )
        self._submit(
            IntentPatch(on=True, brightness=brightness, color_temp_kelvin=kelvin)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._submit(IntentPatch(on=False))

    async def async_toggle(self, **kwargs: Any) -> None:
        if self.is_on:
            await self.async_turn_off(**kwargs)
        else:
            await self.async_turn_on(**kwargs)


class OrchestratorGroupLight(LightEntity):
    """Group entity whose state is derived from its members' own intent."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: Runtime,
        entry_id: str,
        group_id: str,
        members: tuple[str, ...],
        min_kelvin: int,
        max_kelvin: int,
    ) -> None:
        self._runtime = runtime
        self._group_id = group_id
        self._members = members
        self._attr_unique_id = f"{entry_id}_{group_id}"
        self._attr_name = group_id
        self._supports_brightness = False
        self._supports_color_temp = False
        self._config_min_kelvin = min_kelvin
        self._config_max_kelvin = max_kelvin
        self._attr_min_color_temp_kelvin = min_kelvin
        self._attr_max_color_temp_kelvin = max_kelvin
        self._attr_color_mode = ColorMode.ONOFF
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def changed(light_ids: frozenset[str]) -> None:
            if light_ids.intersection(self._members):
                self._refresh_capabilities()
                self.async_write_ha_state()

        self.async_on_remove(self._runtime.subscribe(changed))
        self._refresh_capabilities()

    def _refresh_capabilities(self) -> None:
        get_capabilities = getattr(self._runtime, "group_capabilities", None)
        if get_capabilities is None:
            return
        capabilities = get_capabilities(self._group_id)
        self._supports_brightness = capabilities.brightness_range is not None
        self._supports_color_temp = False
        mode = ColorMode.BRIGHTNESS if self._supports_brightness else ColorMode.ONOFF
        if capabilities.mired_range is not None and self._supports_brightness:
            minimum, maximum = capabilities.mired_range
            low = max(self._config_min_kelvin, round(1_000_000 / maximum))
            high = min(self._config_max_kelvin, round(1_000_000 / minimum))
            if low <= high:
                self._supports_color_temp = True
                self._attr_min_color_temp_kelvin = low
                self._attr_max_color_temp_kelvin = high
                mode = ColorMode.COLOR_TEMP
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    def _common(self, attribute: str) -> int | None:
        values = {
            getattr(state.desired, attribute)
            for item in self._members
            if (state := self._runtime.snapshot(item)).desired.on
        }
        return values.pop() if len(values) == 1 else None

    @property
    def is_on(self) -> bool:
        return self._runtime.group_snapshot(self._group_id).on

    @property
    def available(self) -> bool:
        return self._runtime.group_snapshot(self._group_id).available

    @property
    def brightness(self) -> int | None:
        return self._common("brightness")

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._common("color_temp_kelvin")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self._runtime.group_snapshot(self._group_id)
        return {
            "confirmed_members": snapshot.confirmed_members,
            "member_count": snapshot.member_count,
            "confirmed": snapshot.confirmed,
        }

    def _submit(self, patch: IntentPatch) -> None:
        try:
            self._runtime.submit_group(self._group_id, patch, source="home_assistant")
        except RuntimeError as exc:
            raise HomeAssistantError("Group control is unavailable") from exc

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if kelvin is not None and not self._supports_color_temp:
            raise HomeAssistantError("Color temperature is unsupported")
        if brightness not in (None, 0) and not self._supports_brightness:
            raise HomeAssistantError("Brightness is unsupported")
        if kelvin is not None:
            kelvin = max(
                self.min_color_temp_kelvin, min(self.max_color_temp_kelvin, kelvin)
            )
        self._submit(
            IntentPatch(on=True, brightness=brightness, color_temp_kelvin=kelvin)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._submit(IntentPatch(on=False))
