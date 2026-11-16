"""Physical relay queues and multicast reservations across both group policies."""

# ruff: noqa: F811 - pytest resolves the shared fixture by name.

import pytest

from custom_components.zigbee_led_orchestrator.models import IntentPatch
from tests.unit.test_group_coordinator import GROUP_TOPIC, led, relay, rig  # noqa: F401

pytestmark = pytest.mark.parametrize("rig", ["individual", "wait_all"], indirect=True)


async def test_two_relay_channels_share_one_unresolved_on(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.1)
    assert [p.payload for p in transport.sent] == [{"state_a": "ON"}]
    await clock.advance(5)
    assert len(transport.sent) == 1
    relay(observations, engine, clock, "a")
    await clock.advance(0.1)
    assert [p.payload for p in transport.sent] == [{"state_a": "ON"}, {"state_b": "ON"}]


async def test_multicast_holds_all_member_devices_until_actual_sent_values_match(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.1)
    relay(observations, engine, clock, "a")
    await clock.advance(0.1)
    relay(observations, engine, clock, "b")
    await clock.advance(0.1)
    for item in ("a", "b"):
        led(observations, engine, clock, item, brightness=20)
    await clock.advance(2.6)
    assert [p.topic for p in transport.sent].count(GROUP_TOPIC) == 1
    engine.submit(("a",), IntentPatch(brightness=56), source="slider")
    await clock.advance(0.3)
    led(observations, engine, clock, "a", brightness=100)
    await clock.advance(0.3)
    assert not [p for p in transport.sent if p.topic == "example/led_a/set"]
    led(observations, engine, clock, "b", brightness=100)
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic == "example/led_a/set"] == [
        {"brightness": 56, "transition": 0}
    ]


async def test_merged_off_report_does_not_fail_waiting_second_channel(rig):
    from custom_components.zigbee_led_orchestrator.models import SwitchState

    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True), source="ui")
    await clock.advance(0.1)
    relay(observations, engine, clock, "a")
    relay(observations, engine, clock, "b", SwitchState.OFF)
    await clock.advance(0.5)
    assert [p.payload for p in transport.sent] == [{"state_a": "ON"}, {"state_b": "ON"}]


async def test_member_power_cycle_does_not_lose_settled_multicast_evidence(rig):
    from custom_components.zigbee_led_orchestrator.models import SwitchState

    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.1)
    relay(observations, engine, clock, "a")
    await clock.advance(0.1)
    relay(observations, engine, clock, "b")
    await clock.advance(0.1)
    for item in ("a", "b"):
        led(observations, engine, clock, item, brightness=20)
    await clock.advance(2.6)
    engine.submit(("a",), IntentPatch(on=False), source="ui")
    await clock.advance(0.1)
    relay(observations, engine, clock, "a", SwitchState.OFF)
    await clock.advance(0.1)
    engine.submit(("a",), IntentPatch(on=True, brightness=56), source="ui")
    await clock.advance(0.5)
    relay(observations, engine, clock, "a")
    await clock.advance(0.01)
    led(observations, engine, clock, "a", brightness=20)
    await clock.advance(2.6)
    led(observations, engine, clock, "b", brightness=100)
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic == "example/led_a/set"] == [
        {"state": "ON", "brightness": 56, "transition": 0}
    ]


async def test_queued_multicast_recalculates_delta_after_previous_confirmation(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit(
        "group",
        IntentPatch(on=True, brightness=100, color_temp_kelvin=4000),
        source="ui",
    )
    await clock.advance(0.1)
    relay(observations, engine, clock, "a")
    await clock.advance(0.1)
    relay(observations, engine, clock, "b")
    await clock.advance(0.1)
    for item in ("a", "b"):
        led(observations, engine, clock, item, brightness=20, kelvin=3000)
    await clock.advance(2.6)
    coordinator.submit("group", IntentPatch(brightness=56), source="ui")
    await clock.advance(0.3)
    for item in ("a", "b"):
        led(observations, engine, clock, item, brightness=100, kelvin=4000)
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic == GROUP_TOPIC][-1] == {
        "brightness": 56,
        "transition": 0,
    }
