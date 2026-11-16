"""A broker accepts immediately while device execution is still unresolved."""

# ruff: noqa: F811 - pytest uses the imported fixture by its registered name.

import pytest

from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    SwitchState,
)
from tests.unit.test_reconciler import led, relay, rig  # noqa: F401

pytestmark = pytest.mark.parametrize("rig", [True], indirect=True)


async def powered(rig):
    clock, transport, observations, engine = rig
    relay(observations, engine, clock, SwitchState.OFF)
    engine.submit(
        ("led",),
        IntentPatch(on=True, brightness=100, color_temp_kelvin=3000),
        source="ui",
    )
    await clock.advance(0.41)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=100, kelvin=3000)
    await clock.advance(2.6)
    return clock, transport, observations, engine


async def test_only_first_and_latest_slider_values_reach_the_device(rig):
    clock, transport, observations, engine = await powered(rig)
    transport.sent.clear()
    for value in (255, 56, 191, 56):
        engine.submit(("led",), IntentPatch(on=True, brightness=value), source="slider")
        await clock.advance(0.3)
    sets = [x.payload for x in transport.sent if x.topic == "example/led/set"]
    assert sets == [{"brightness": 255, "transition": 0}]
    await clock.advance(5)
    assert len([x for x in transport.sent if x.topic.endswith("/get")]) <= 1
    led(observations, engine, clock, brightness=255, kelvin=3000)
    await clock.advance(0.3)
    assert [x.payload for x in transport.sent if x.topic == "example/led/set"] == [
        {"brightness": 255, "transition": 0},
        {"brightness": 56, "transition": 0},
    ]
    assert engine.snapshot("led").desired.brightness == 56


async def test_repeated_off_does_not_repeat_unresolved_cutoff(rig):
    clock, transport, observations, engine = await powered(rig)
    transport.sent.clear()
    for _ in range(5):
        engine.submit(("led",), IntentPatch(on=False), source="ui")
        await clock.advance(3.5)
    assert [x.payload for x in transport.sent if x.topic == "example/relay/set"] == [
        {"state_l1": "OFF"}
    ]
    assert [x.payload for x in transport.sent if x.topic == "example/led/set"] == [
        {"state": "OFF", "transition": 0}
    ]
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    assert engine.snapshot("led").phase.value == "OFF"


async def test_off_bypasses_unresolved_parameters_and_keeps_original_deadline(rig):
    clock, transport, _observations, engine = await powered(rig)
    engine.submit(("led",), IntentPatch(brightness=255), source="slider")
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(brightness=56), source="slider")
    await clock.advance(0.3)
    start = clock.now()
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(2.1)
    cuts = [x for x in transport.sent if x.payload == {"state_l1": "OFF"}]
    assert len(cuts) == 1
    assert cuts[0].sent_at - start <= 2.01
    assert not any(x.payload.get("brightness") == 56 for x in transport.sent)


async def test_partial_report_cannot_confirm_other_cached_attributes(rig):
    clock, transport, observations, engine = await powered(rig)
    engine.submit(
        ("led",), IntentPatch(brightness=200, color_temp_kelvin=4000), source="ui"
    )
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(brightness=56), source="ui")
    await clock.advance(0.3)
    before = len([x for x in transport.sent if x.topic.endswith("/set")])
    engine.notify(
        observations.ingest(
            LedObservation(
                "led",
                SwitchState.ON,
                clock.now(),
                False,
                EvidenceQuality.DEVICE_REPORT,
                200,
                4000,
                (
                    ("state", clock.now()),
                    ("brightness", clock.now()),
                    ("color_temp", 1.5),
                ),
            )
        )
    )
    await clock.advance(0.3)
    assert len([x for x in transport.sent if x.topic.endswith("/set")]) == before
    led(observations, engine, clock, brightness=200, kelvin=4000)
    await clock.advance(0.3)
    assert [x.payload for x in transport.sent if x.topic == "example/led/set"][-1] == {
        "brightness": 56,
        "transition": 0,
    }


async def test_new_on_after_off_timeout_waits_for_late_cutoff_then_resumes(rig):
    clock, transport, observations, engine = await powered(rig)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(6)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(4)
    count = len(transport.sent)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.5)
    assert engine.snapshot("led").desired.on
    assert [x.payload for x in transport.sent[count:]] == [{"state_l1": "ON"}]


async def test_goal_already_reached_while_waiting_needs_no_empty_set(rig):
    clock, transport, observations, engine = await powered(rig)
    transport.sent.clear()
    engine.submit(("led",), IntentPatch(brightness=255), source="ui")
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(brightness=56), source="ui")
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(brightness=255), source="ui")
    await clock.advance(0.3)
    led(observations, engine, clock, brightness=255, kelvin=3000)
    await clock.advance(0.3)
    assert [x.payload for x in transport.sent if x.topic.endswith("/set")] == [
        {"brightness": 255, "transition": 0}
    ]
    assert engine.snapshot("led").confirmed


async def test_late_relay_on_resumes_latest_goal_after_confirmation_timeout(rig):
    clock, transport, observations, engine = rig
    relay(observations, engine, clock, SwitchState.OFF)
    engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(5)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=20)
    await clock.advance(2.7)
    assert [x.payload for x in transport.sent if x.topic == "example/led/set"] == [
        {"state": "ON", "brightness": 100, "transition": 0}
    ]


async def test_final_read_waits_for_previous_read_response_and_uses_sent_fields(rig):
    clock, transport, observations, engine = await powered(rig)
    transport.sent.clear()
    engine.submit(("led",), IntentPatch(brightness=200), source="ui")
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(color_temp_kelvin=4000), source="ui")
    await clock.advance(0.3)
    engine.notify(
        observations.ingest(
            LedObservation(
                "led",
                SwitchState.ON,
                clock.now(),
                False,
                EvidenceQuality.DEVICE_REPORT,
                200,
                3000,
                read_response_times=(),
            )
        )
    )
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic.endswith("/get")] == [
        {"brightness": ""}
    ]
    engine.notify(
        observations.ingest(
            LedObservation(
                "led",
                SwitchState.ON,
                clock.now(),
                False,
                EvidenceQuality.DEVICE_REPORT,
                200,
                3000,
                read_response_times=(("brightness", clock.now()),),
            )
        )
    )
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic.endswith("/get")] == [
        {"brightness": ""},
        {"color_temp": ""},
    ]


async def test_late_matching_evidence_resolves_unknown_parameter_publication(rig):
    clock, transport, observations, engine = await powered(rig)
    transport.fail_next(TimeoutError())
    engine.submit(("led",), IntentPatch(brightness=200), source="ui")
    await clock.advance(0.3)
    engine.submit(("led",), IntentPatch(brightness=56), source="ui")
    await clock.advance(0.3)
    led(observations, engine, clock, brightness=200, kelvin=3000)
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic == "example/led/set"][-1] == {
        "brightness": 56,
        "transition": 0,
    }


async def test_late_off_proof_resumes_new_on_after_unknown_cutoff(rig):
    clock, transport, observations, engine = await powered(rig)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(1.9)
    transport.fail_next(TimeoutError())
    await clock.advance(1)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.1)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.5)
    assert transport.sent[-1].payload == {"state_l1": "ON"}


async def test_on_after_accepted_led_off_requires_full_on_even_with_cached_on(rig):
    clock, transport, _observations, engine = await powered(rig)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.1)
    engine.submit(("led",), IntentPatch(on=True, color_temp_kelvin=4000), source="ui")
    await clock.advance(0.3)
    assert [p.payload for p in transport.sent if p.topic == "example/led/set"][-1] == {
        "state": "ON",
        "brightness": 100,
        "color_temp": 250,
        "transition": 0,
    }
