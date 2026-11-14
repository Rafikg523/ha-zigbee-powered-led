"""Entry-targeted services with explicit effects; none publishes to devices."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

SERVICES = ("pause", "resume", "resync", "diagnose")
_BASE = {vol.Required("config_entry_id"): cv.string}
_DIAGNOSE = {**_BASE, vol.Optional("light_id"): cv.string}


def _runtime(hass: HomeAssistant, call: ServiceCall):
    entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
    runtime = getattr(entry, "runtime_data", None)
    if entry is None or entry.domain != DOMAIN or runtime is None:
        raise ServiceValidationError("Unknown or unloaded config entry")
    return runtime


def async_register_services(hass: HomeAssistant) -> None:
    async def pause(call: ServiceCall) -> None:
        try:
            _runtime(hass, call).pause()
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def resume(call: ServiceCall) -> None:
        try:
            _runtime(hass, call).resume()
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def resync(call: ServiceCall) -> None:
        try:
            await _runtime(hass, call).resync()
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def diagnose(call: ServiceCall) -> dict[str, object]:
        try:
            return _runtime(hass, call).diagnostics(call.data.get("light_id"))
        except KeyError as exc:
            raise ServiceValidationError("Unknown light") from exc

    hass.services.async_register(DOMAIN, "pause", pause, vol.Schema(_BASE))
    hass.services.async_register(DOMAIN, "resume", resume, vol.Schema(_BASE))
    hass.services.async_register(DOMAIN, "resync", resync, vol.Schema(_BASE))
    hass.services.async_register(
        DOMAIN,
        "diagnose",
        diagnose,
        vol.Schema(_DIAGNOSE),
        supports_response=SupportsResponse.ONLY,
    )


def async_remove_services(hass: HomeAssistant) -> None:
    for name in SERVICES:
        hass.services.async_remove(DOMAIN, name)
