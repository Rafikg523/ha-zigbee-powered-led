"""Home Assistant diagnostics export with identifiers redacted."""

from __future__ import annotations


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, object]:
    return entry.runtime_data.diagnostics()
