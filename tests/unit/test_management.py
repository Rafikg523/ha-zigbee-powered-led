"""Editing stored circuits and groups keeps the configuration valid."""

import pytest

from custom_components.zigbee_led_orchestrator.management import (
    CircuitInGroupError,
    group_of,
    without_circuit,
    without_group,
)
from tests.unit.test_configuration import valid_config


def test_grouped_circuit_is_not_removed():
    data = valid_config()
    with pytest.raises(CircuitInGroupError) as caught:
        without_circuit(data, "light_a")
    assert caught.value.group_name == "group_a"
    assert data == valid_config()


def test_removing_a_group_keeps_its_circuits_and_drops_empty_rooms():
    data = valid_config()
    updated = without_group(data, "group_a")
    assert updated["groups"] == []
    assert updated["rooms"] == []
    assert [item["id"] for item in updated["lights"]] == ["light_a", "light_b"]
    assert data == valid_config()


def test_free_circuit_is_removed_with_its_own_relay_channel_only():
    data = without_group(valid_config(), "group_a")
    updated = without_circuit(data, "light_a")
    assert [item["id"] for item in updated["lights"]] == ["light_b"]
    assert [item["id"] for item in updated["relay_channels"]] == ["relay_b"]
    assert group_of(updated, "light_b") is None


def test_unknown_items_are_reported():
    with pytest.raises(KeyError):
        without_circuit(valid_config(), "light_x")
    with pytest.raises(KeyError):
        without_group(valid_config(), "group_x")
