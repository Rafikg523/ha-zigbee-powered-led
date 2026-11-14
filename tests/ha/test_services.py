"""Services have explicit effects and never publish to devices."""

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zigbee_led_orchestrator.const import DOMAIN
from custom_components.zigbee_led_orchestrator.models import (
    BridgeObservation,
    ControlMode,
    EvidenceQuality,
    IntentPatch,
    RelayObservation,
    SwitchState,
)
from tests.ha.test_init import mocked_adapter  # noqa: F401
from tests.unit.test_configuration import valid_config


@pytest.fixture
async def setup(hass, mqtt_mock, mocked_adapter, enable_custom_integrations):  # noqa: F811
    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    adapter = mocked_adapter[0]
    now = runtime._clock.now()
    adapter.observation(
        BridgeObservation(True, now, False, EvidenceQuality.UNKNOWN_SOURCE)
    )
    for channel in ("relay_a", "relay_b"):
        adapter.observation(
            RelayObservation(
                channel,
                SwitchState.OFF,
                now + 0.001,
                False,
                EvidenceQuality.DEVICE_REPORT,
            )
        )
    yield hass, entry, runtime, adapter
    await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def call(hass, entry, service, **data):
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"config_entry_id": entry.entry_id, **data},
        blocking=True,
        return_response=service == "diagnose",
    )


async def test_pause_resume_never_replays(setup):
    hass, entry, runtime, adapter = setup
    runtime.set_mode(ControlMode.ACTIVE)
    runtime.submit(("light_a",), IntentPatch(on=True), source="ui")
    await hass.async_block_till_done()
    sent = list(adapter.sent)
    await call(hass, entry, "pause")
    assert runtime.mode == ControlMode.PAUSED
    assert not adapter.active
    with pytest.raises(RuntimeError):
        runtime.submit(("light_a",), IntentPatch(on=True), source="ui")
    await call(hass, entry, "resume")
    await hass.async_block_till_done()
    assert runtime.mode == ControlMode.ACTIVE
    assert adapter.sent == sent
    assert not runtime.snapshot("light_a").desired.on


async def test_resume_never_promotes_observe_to_active(setup):
    hass, entry, runtime, adapter = setup
    runtime.set_mode(ControlMode.OBSERVE, manual=True)
    await call(hass, entry, "pause")
    assert runtime.mode == ControlMode.PAUSED
    await call(hass, entry, "resume")
    assert runtime.mode == ControlMode.OBSERVE
    assert not adapter.active
    assert not adapter.sent


async def test_resume_requires_pause(setup):
    hass, entry, _runtime, _ = setup
    with pytest.raises(HomeAssistantError):
        await call(hass, entry, "resume")


async def test_resync_does_not_power_on(setup):
    hass, entry, runtime, adapter = setup
    runtime.set_mode(ControlMode.ACTIVE)
    await call(hass, entry, "resync")
    await hass.async_block_till_done()
    assert adapter.resynced == 1
    assert not adapter.sent
    assert runtime.mode == ControlMode.OBSERVE
    assert not adapter.active
    assert runtime._observations.bridge_online is None
    assert not runtime.snapshot("light_a").confirmed
    runtime.set_mode(ControlMode.ACTIVE)
    assert not adapter.sent


async def test_resync_while_paused_stays_paused(setup):
    hass, entry, runtime, adapter = setup
    runtime.set_mode(ControlMode.ACTIVE)
    await call(hass, entry, "pause")
    await call(hass, entry, "resync")
    assert runtime.mode == ControlMode.PAUSED
    assert not adapter.sent


async def test_diagnose_returns_redacted_data(setup):
    hass, entry, _runtime, adapter = setup
    result = await call(hass, entry, "diagnose")
    assert result["mode"] == "ACTIVE"
    assert "0x0000000000000001" not in str(result)
    single = await call(hass, entry, "diagnose", light_id="light_a")
    assert len(single["lights"]) == 1
    assert not adapter.sent


async def test_unknown_entry_or_light_is_rejected(setup):
    hass, entry, _runtime, _ = setup
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "pause", {"config_entry_id": "missing"}, blocking=True
        )
    with pytest.raises(HomeAssistantError):
        await call(hass, entry, "diagnose", light_id="unknown")


async def test_services_are_removed_on_unload(
    hass,
    mqtt_mock,
    mocked_adapter,  # noqa: F811
    enable_custom_integrations,
):
    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "pause")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "pause")
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()
