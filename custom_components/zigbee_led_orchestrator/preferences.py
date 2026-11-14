"""Validated, power-free preference persistence."""

from __future__ import annotations

from dataclasses import dataclass

from .models import OrchestratorConfig

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Preference:
    """Parameters to keep for a light; absence of ON is deliberate."""

    brightness: int | None = None
    color_temp_kelvin: int | None = None


def serialize_preferences(preferences: dict[str, Preference]) -> dict[str, object]:
    lights = {}
    for light_id, item in preferences.items():
        entry = {}
        if item.brightness is not None:
            entry["brightness"] = item.brightness
        if item.color_temp_kelvin is not None:
            entry["color_temp_kelvin"] = item.color_temp_kelvin
        if entry:
            lights[light_id] = entry
    return {"version": SCHEMA_VERSION, "lights": lights}


def _parse(raw: object, config: OrchestratorConfig) -> dict[str, Preference]:
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported preferences schema")
    lights = raw.get("lights")
    if not isinstance(lights, dict) or set(raw) != {"version", "lights"}:
        raise ValueError("invalid preferences structure")
    known = {light.id: light for light in config.lights}
    result: dict[str, Preference] = {}
    for light_id, entry in lights.items():
        if light_id not in known:
            raise ValueError("preferences reference an unknown light")
        if not isinstance(entry, dict) or not set(entry) <= {
            "brightness",
            "color_temp_kelvin",
        }:
            raise ValueError("invalid light preferences")
        brightness = entry.get("brightness")
        kelvin = entry.get("color_temp_kelvin")
        if brightness is not None and (
            type(brightness) is not int or not 1 <= brightness <= 255
        ):
            raise ValueError("invalid brightness preference")
        light = known[light_id]
        if kelvin is not None and (
            type(kelvin) is not int
            or not light.min_kelvin <= kelvin <= light.max_kelvin
        ):
            raise ValueError("invalid color temperature preference")
        result[light_id] = Preference(brightness, kelvin)
    return result


def load_preferences(
    raw: object, config: OrchestratorConfig
) -> tuple[dict[str, Preference], str | None]:
    """Return preferences, or empty preferences and a reason for a bad record."""
    if raw is None:
        return {}, None
    try:
        return _parse(raw, config), None
    except ValueError as exc:
        return {}, str(exc)


class PreferencesStore:
    """Home Assistant storage for the single configured instance."""

    def __init__(self, hass, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, SCHEMA_VERSION, f"zigbee_led_orchestrator.{entry_id}")

    async def async_load(self) -> object:
        return await self._store.async_load()

    async def async_save(self, preferences: dict[str, Preference]) -> None:
        await self._store.async_save(serialize_preferences(preferences))

    def schedule_save(self, preferences) -> None:
        self._store.async_delay_save(lambda: serialize_preferences(preferences()), 2.0)
