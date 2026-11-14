"""Diagnostic entities separate request, phase and confirmation."""

from homeassistant.helpers import entity_registry as er

from tests.ha.test_init import mocked_adapter  # noqa: F401
from tests.ha.test_services import setup  # noqa: F401


def _state(hass, entry, unique_suffix):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        None, "zigbee_led_orchestrator", f"{entry.entry_id}_{unique_suffix}"
    )
    if entity_id is None:
        for platform in ("sensor", "binary_sensor"):
            entity_id = registry.async_get_entity_id(
                platform, "zigbee_led_orchestrator", f"{entry.entry_id}_{unique_suffix}"
            )
            if entity_id:
                break
    assert entity_id is not None
    return hass.states.get(entity_id)


async def test_diagnostic_entities_report_without_control(setup):  # noqa: F811
    hass, entry, _runtime, _adapter = setup
    registry = er.async_get(hass)
    for suffix in ("light_a_phase", "light_a_effective_state", "light_a_confirmed"):
        state = _state(hass, entry, suffix)
        assert state is not None
        assert state.state != "unavailable"
    confirmed = _state(hass, entry, "light_a_confirmed")
    # Bridge online arms control, so a trusted OFF report confirms the off state.
    assert confirmed.state == "on"
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    diagnostic = [e for e in entries if e.unique_id.endswith(("phase", "confirmed"))]
    assert diagnostic
    assert all(e.entity_category is not None for e in diagnostic)
