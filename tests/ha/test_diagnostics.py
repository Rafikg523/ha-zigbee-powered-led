"""Diagnostic exports redact installation identifiers."""

import json
import re
from types import SimpleNamespace

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.models import ControlMode, IntentPatch
from tests.ha.test_runtime import FakeAdapter
from tests.unit.test_configuration import valid_config

SENSITIVE = (
    "0x0000000000000001",
    "0x0000000000000002",
    "0x0000000000000003",
    "zigbee2mqtt",
    "light_a",
    "light_b",
    "relay_a",
    "relay_b",
    "group_a",
    "room_a",
    "relay",
    "led1",
    "led2",
    "group",
)


@pytest.fixture
async def runtime():
    from custom_components.zigbee_led_orchestrator.runtime import Runtime

    adapter = FakeAdapter()
    instance = Runtime(validate_config(valid_config()), adapter)
    await instance.start()
    yield instance, adapter
    await instance.close()


def assert_redacted(text):
    for item in SENSITIVE:
        assert not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(item)}(?![A-Za-z0-9_])", text
        )


async def test_diagnostics_redacts_identifiers(runtime):
    instance, _adapter = runtime
    instance.set_mode(ControlMode.ACTIVE)
    instance.submit(("light_a",), IntentPatch(on=True), source="ui")
    data = instance.diagnostics()
    text = json.dumps(data)
    assert_redacted(text)
    assert data["mode"] == "ACTIVE"
    assert len(data["lights"]) == 2
    assert {"phase", "effective_state", "confirmed", "desired"} <= set(
        next(iter(data["lights"].values()))
    )
    assert data["counters"]["commands_published"] >= 0
    assert data["preferences_error"] is None
    assert data["control_notice"]


async def test_diagnostics_scrubs_identifiers_in_reasons_and_errors(runtime):
    instance, _adapter = runtime
    instance.preferences_error = "bad value for light_a at zigbee2mqtt/relay"
    text = json.dumps(instance.diagnostics())
    assert_redacted(text)
    assert instance.diagnostics()["preferences_error"]


async def test_config_entry_diagnostics_use_runtime(hass, runtime):
    from custom_components.zigbee_led_orchestrator.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    instance, _ = runtime
    entry = SimpleNamespace(runtime_data=instance)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert_redacted(json.dumps(data))
    assert data["mode"] == "OBSERVE"


async def test_single_light_filter_does_not_reveal_other_lights(runtime):
    instance, _ = runtime
    data = instance.diagnostics("light_a")
    assert len(data["lights"]) == 1
    with pytest.raises(KeyError):
        instance.diagnostics("unknown")
