"""Synthetic config-entry lifecycle tests."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zigbee_led_orchestrator.const import DOMAIN
from tests.ha.test_runtime import FakeAdapter
from tests.unit.test_configuration import valid_config


@pytest.fixture
def mocked_adapter(monkeypatch):
    adapters = []

    def factory(hass, config):
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.mqtt_adapter.MqttAdapter", factory
    )
    return adapters


async def test_setup_is_observe_only_and_unload_cleans_runtime(
    hass, mocked_adapter, monkeypatch
):
    from custom_components.zigbee_led_orchestrator import (
        async_setup_entry,
        async_unload_entry,
    )

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, entry)
    assert not mocked_adapter[0].active
    assert not mocked_adapter[0].sent
    assert await async_unload_entry(hass, entry)
    assert mocked_adapter[0].closed
    assert mocked_adapter[0].topology_callback is None


async def test_setup_ignores_obsolete_control_mode_option(
    hass, mocked_adapter, monkeypatch
):
    from custom_components.zigbee_led_orchestrator import (
        async_setup_entry,
        async_unload_entry,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data=valid_config(), options={"active": False}
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, entry)
    entry.runtime_data._maybe_arm()
    assert not mocked_adapter[0].active
    assert not entry.runtime_data._user_inactive
    assert await async_unload_entry(hass, entry)


async def test_restart_with_pending_group_change_keeps_group_unavailable(
    hass, mocked_adapter, monkeypatch
):
    from custom_components.zigbee_led_orchestrator import (
        async_setup_entry,
        async_unload_entry,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=valid_config(),
        options={"pending_group_operation": {"action": "edit", "id": "group_a"}},
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, entry)
    assert not entry.runtime_data.group_snapshot("group_a").available
    assert not mocked_adapter[0].sent
    assert await async_unload_entry(hass, entry)


async def test_second_entry_is_rejected(hass, mocked_adapter, monkeypatch):
    from custom_components.zigbee_led_orchestrator import (
        async_setup_entry,
        async_unload_entry,
    )

    first = MockConfigEntry(domain=DOMAIN, data=valid_config())
    second = MockConfigEntry(domain=DOMAIN, data=valid_config())
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, first)
    assert not await async_setup_entry(hass, second)
    assert len(mocked_adapter) == 1
    await async_unload_entry(hass, first)


async def test_failed_setup_cleans_partial_runtime(hass, mocked_adapter, monkeypatch):
    from custom_components.zigbee_led_orchestrator import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=RuntimeError("failed")),
    )
    with pytest.raises(RuntimeError, match="failed"):
        await async_setup_entry(hass, entry)
    assert mocked_adapter[0].closed
    assert hass.data.get(DOMAIN) is None


async def test_home_assistant_creates_lights_and_unloads(
    hass, mqtt_mock, mocked_adapter, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entries) == 9
    assert {item.unique_id for item in entries} == {
        f"{entry.entry_id}_light_a",
        f"{entry.entry_id}_light_b",
        f"{entry.entry_id}_group_a",
        f"{entry.entry_id}_light_a_phase",
        f"{entry.entry_id}_light_b_phase",
        f"{entry.entry_id}_light_a_effective_state",
        f"{entry.entry_id}_light_b_effective_state",
        f"{entry.entry_id}_light_a_confirmed",
        f"{entry.entry_id}_light_b_confirmed",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert mocked_adapter[0].closed
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def test_existing_light_keeps_entity_id_and_user_name_when_device_is_added(
    hass, mqtt_mock, mocked_adapter, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, data=valid_config())
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "light",
        DOMAIN,
        f"{entry.entry_id}_light_a",
        config_entry=entry,
        suggested_object_id="custom_old_light",
    )
    registry.async_update_entity(old.entity_id, name="My custom name")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    current = registry.async_get(old.entity_id)
    assert current is not None
    assert current.name == "My custom name"
    assert current.device_id is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def test_group_control_entity_does_not_reuse_the_z2m_group_name(
    hass, mqtt_mock, mocked_adapter, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    from custom_components.zigbee_led_orchestrator.group_provisioner import (
        group_control_label,
    )

    data = valid_config()
    data["groups"][0]["name"] = "Salon"
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    z2m_entity = registry.async_get_or_create(
        "light",
        "mqtt",
        "z2m_salon_group",
        suggested_object_id="salon_orchestrator",
    )
    assert z2m_entity.entity_id == "light.salon_orchestrator"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert z2m_entity.entity_id == "light.salon_orchestrator"
    group_entity_id = registry.async_get_entity_id(
        "light", DOMAIN, f"{entry.entry_id}_group_a"
    )
    assert group_entity_id is not None
    assert group_entity_id != z2m_entity.entity_id
    assert group_entity_id != f"{z2m_entity.entity_id}_2"
    assert group_entity_id == "light.salon_orchestrator_control"
    assert group_entity_id.endswith(f"_{group_control_label(hass.config.language)}")
    assert group_entity_id != "light.salon_orchestrator"

    assert await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()


async def test_two_ha_toggles_during_boot_cancel_pending_on(
    hass, mqtt_mock, mocked_adapter, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    from custom_components.zigbee_led_orchestrator.models import (
        BridgeObservation,
        ControlMode,
        EvidenceQuality,
        RelayObservation,
        SwitchState,
    )

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
    adapter.observation(
        RelayObservation(
            "relay_a",
            SwitchState.OFF,
            now + 0.001,
            False,
            EvidenceQuality.DEVICE_REPORT,
        )
    )
    runtime.set_mode(ControlMode.ACTIVE)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "light", DOMAIN, f"{entry.entry_id}_light_a"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert runtime.snapshot("light_a").desired.on
    await hass.services.async_call(
        "light", "toggle", {"entity_id": entity_id}, blocking=True
    )
    assert not runtime.snapshot("light_a").desired.on
    assert all(payload.get("state_l1") != "ON" for _, payload, _ in adapter.sent)
    assert await hass.config_entries.async_unload(entry.entry_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    await hass.config_entries.async_unload(mqtt_entry.entry_id)
    if mqtt_mock._mock_wraps._misc_timer is not None:
        mqtt_mock._mock_wraps._misc_timer.cancel()
