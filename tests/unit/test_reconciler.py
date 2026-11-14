"""Single-circuit behavior using only simulated time and transport."""

import pytest

from custom_components.zigbee_led_orchestrator.desired_store import DesiredStateStore
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    Phase,
    PolicyConfig,
    RelayObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.observation_store import ObservationStore
from tests.helpers.clock import FakeClock
from tests.helpers.transport import FakeTransport


@pytest.fixture
async def rig():
    from custom_components.zigbee_led_orchestrator.reconciler import Circuit, Reconciler

    clock = FakeClock(start=1)
    transport = FakeTransport(clock)
    desired = DesiredStateStore(("led",), clock=clock)
    observations = ObservationStore({"led": "relay"})
    circuit = Circuit(
        light_id="led",
        relay_channel_id="relay",
        relay_topic="example/relay/set",
        led_topic="example/led/set",
        relay_resource="synthetic:relay:channel1",
        led_resource="synthetic:led:endpoint1",
        relay_state_key="state_l1",
    )
    policy = PolicyConfig(min_send_interval_ms=1, publication_timeout_ms=500)
    engine = Reconciler(
        circuits=(circuit,),
        desired=desired,
        observations=observations,
        transport=transport,
        clock=clock,
        policy=policy,
        enabled=True,
    )
    observations.ingest(
        RelayObservation(
            "relay", SwitchState.OFF, 0, False, EvidenceQuality.DEVICE_REPORT
        )
    )
    yield clock, transport, observations, engine
    await engine.close()


def relay(observations, engine, clock, state):
    delta = observations.ingest(
        RelayObservation(
            "relay", state, clock.now(), False, EvidenceQuality.DEVICE_REPORT
        )
    )
    engine.notify(delta)


def led(observations, engine, clock, *, brightness=None, kelvin=None):
    delta = observations.ingest(
        LedObservation(
            "led",
            SwitchState.ON,
            clock.now(),
            False,
            EvidenceQuality.DEVICE_REPORT,
            brightness=brightness,
            color_temp_kelvin=kelvin,
        )
    )
    engine.notify(delta)


async def test_t01_on_waits_for_relay_and_readiness(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=180), source="ui")
    await clock.advance(0.01)
    assert [(x.topic, x.payload) for x in transport.sent] == [
        ("example/relay/set", {"state_l1": "ON"})
    ]
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.49)
    assert len(transport.sent) == 1
    await clock.advance(0.13)
    assert [(x.topic, x.payload) for x in transport.sent] == [
        ("example/relay/set", {"state_l1": "ON"}),
        ("example/led/set", {"state": "ON", "brightness": 180, "transition": 0}),
    ]
    assert engine.snapshot("led").phase == Phase.UNCONFIRMED


async def test_t02_off_skips_led_off(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    assert transport.sent[-1].payload == {"state_l1": "OFF"}
    assert all(x.topic != "example/led/set" for x in transport.sent)


async def test_t03_off_during_boot_cancels_on(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(10)
    assert engine.snapshot("led").desired.on is False
    assert all(x.topic != "example/led/set" for x in transport.sent)


async def test_missing_relay_confirmation_blocks_led_set(rig):
    clock, transport, _, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(3.1)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.ERROR


async def test_t16_slow_boot_does_not_repeat_set(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=160), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(3)
    assert len(transport.sent) == 1
    led(observations, engine, clock)
    await clock.advance(0.13)
    assert len(transport.sent) == 2
    await clock.advance(3)
    assert len(transport.sent) == 2
    assert engine.snapshot("led").phase == Phase.UNCONFIRMED


async def test_t04_twenty_slider_updates_coalesce_during_relay_confirmation(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=1), source="ui")
    await clock.advance(0.01)
    for value in range(2, 22):
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
        await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [{"state_l1": "ON"}]
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.7)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state": "ON", "brightness": 21, "transition": 0},
    ]


async def test_on_after_sent_off_waits_for_confirmation_and_minimum_off_time(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state_l1": "OFF"},
    ]
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.399)
    assert len(transport.sent) == 2
    await clock.advance(0.002)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state_l1": "OFF"},
        {"state_l1": "ON"},
    ]


async def test_fresh_matching_led_state_skips_unnecessary_set(rig):
    clock, transport, observations, engine = rig
    engine.submit(
        ("led",),
        IntentPatch(on=True, brightness=140, color_temp_kelvin=4000),
        source="ui",
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=140, kelvin=4000)
    await clock.advance(2.6)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.ON


async def test_led_report_before_send_cannot_confirm_later_command(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=140), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=100)
    await clock.advance(2.5)
    led(observations, engine, clock, brightness=140)
    await clock.advance(0.13)
    assert len(transport.sent) == 2
    assert engine.snapshot("led").phase == Phase.UNCONFIRMED


async def test_power_epoch_change_during_boot_never_sends_old_led_intent(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=90), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(3)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.ON


async def test_explicit_off_with_unchanged_desired_and_observed_on_sends_off(rig):
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [{"state_l1": "OFF"}]


async def test_t04_slider_after_start_sends_only_latest_setting(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=100)
    await clock.advance(2.6)
    assert engine.snapshot("led").phase == Phase.ON
    for value in range(1, 21):
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
        await clock.advance(0.01)
    await clock.advance(0.12)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state": "ON", "brightness": 20, "transition": 0},
    ]


async def test_snapshot_does_not_confirm_old_relay_off_for_new_on(rig):
    _, _, _, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    assert engine.snapshot("led").confirmed is False


async def test_slider_updates_do_not_extend_relay_confirmation_deadline(rig):
    clock, transport, _, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.1)
    for value in range(1, 11):
        await clock.advance(0.25)
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
    await clock.advance(0.5)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.ERROR
    assert engine.snapshot("led").reason == "relay_confirmation_timeout"


async def test_external_off_is_held_for_minimum_before_on(rig):
    clock, transport, observations, engine = rig
    # No OFF was requested through the reconciler: the relay changed externally.
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.399)
    assert not transport.sent
    await clock.advance(0.002)
    assert [p.payload for p in transport.sent] == [{"state_l1": "ON"}]


async def test_first_off_observation_is_not_a_known_physical_transition(rig):
    _, _, observations, _ = rig
    snapshot = observations.snapshot("led")
    assert snapshot.power_off_at is None
    assert snapshot.relay_received_at == 0


async def test_reconciler_uses_shared_state_machine_events(rig, monkeypatch):
    from custom_components.zigbee_led_orchestrator.state_machine import (
        Event,
        StateMachine,
    )

    clock, _, observations, engine = rig
    calls = []
    original = StateMachine.advance

    def track(machine, event):
        calls.append(event)
        return original(machine, event)

    monkeypatch.setattr(StateMachine, "advance", track)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.6)
    assert calls == [
        Event.POWER_REQUESTED,
        Event.POWER_CONFIRMED,
        Event.READY,
        Event.LED_CONFIRMED,
    ]
    assert engine.snapshot("led").phase == Phase.ON


async def test_first_off_after_restart_waits_conservatively_from_observation(rig):
    from custom_components.zigbee_led_orchestrator.models import BridgeObservation

    clock, transport, observations, engine = rig
    observations.invalidate_bridge()
    observations.ingest(
        BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
    )
    relay(observations, engine, clock, SwitchState.OFF)
    assert observations.snapshot("led").power_off_at is None
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.399)
    assert not transport.sent
    await clock.advance(0.002)
    assert [p.payload for p in transport.sent] == [{"state_l1": "ON"}]


async def test_repeated_off_report_does_not_reset_observed_transition(rig):
    clock, _, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    off_observed_at = observations.snapshot("led").power_off_at
    await clock.advance(0.1)
    relay(observations, engine, clock, SwitchState.OFF)
    assert observations.snapshot("led").power_off_at == off_observed_at


async def test_untrusted_on_invalidates_prior_known_off_transition(rig):
    clock, _, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    assert observations.snapshot("led").power_off_at is not None
    await clock.advance(0.01)
    observations.ingest(
        RelayObservation(
            "relay", SwitchState.ON, clock.now(), False, EvidenceQuality.UNKNOWN_SOURCE
        )
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    assert observations.snapshot("led").power_off_at is None


async def test_slider_updates_do_not_extend_readiness_deadline(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    for value in range(1, 9):
        await clock.advance(0.9)
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
    await clock.advance(0.81)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.ERROR
    assert engine.snapshot("led").reason == "readiness_timeout"


async def test_boot_grace_boundary_requires_evidence_and_full_interval(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=120), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=100)
    await clock.advance(2.489)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").phase == Phase.WAITING_READY
    await clock.advance(0.002)
    assert engine.snapshot("led").phase == Phase.APPLYING
    await clock.advance(0.12)
    assert len(transport.sent) == 2


async def test_readiness_timeout_boundary_without_led_evidence(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(7.999)
    assert engine.snapshot("led").phase == Phase.WAITING_READY
    await clock.advance(0.002)
    assert engine.snapshot("led").phase == Phase.ERROR
    assert engine.snapshot("led").reason == "readiness_timeout"
    assert len(transport.sent) == 1


async def test_parameter_changes_while_turning_off_do_not_repeat_relay_off(rig):
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    for value in range(1, 6):
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
        await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [{"state_l1": "OFF"}]
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    assert engine.snapshot("led").phase == Phase.OFF


async def test_off_preference_change_does_not_publish(rig):
    clock, transport, _, engine = rig
    engine.submit(("led",), IntentPatch(brightness=42), source="slider")
    await clock.advance(1)
    assert not transport.sent
    assert engine.snapshot("led").desired.brightness == 42


async def test_parameter_changes_do_not_extend_off_confirmation_deadline(rig):
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    for value in range(1, 11):
        await clock.advance(0.25)
        engine.submit(("led",), IntentPatch(brightness=value), source="slider")
    await clock.advance(0.5)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").reason == "relay_confirmation_timeout"


async def test_zero_brightness_turns_off_an_on_light(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.6)
    engine.submit(("led",), IntentPatch(brightness=0), source="ui")
    await clock.advance(0.01)
    assert transport.sent[-1].payload == {"state_l1": "OFF"}


async def test_new_off_after_previous_confirmation_sends_again(rig):
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "OFF"},
        {"state_l1": "OFF"},
    ]


async def test_on_after_unconfirmed_off_waits_for_off_evidence(rig):
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=10)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(3.1)
    assert engine.snapshot("led").reason == "relay_confirmation_timeout"
    engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.2)
    assert [p.payload for p in transport.sent] == [{"state_l1": "OFF"}]


async def test_suspending_control_revokes_pending_on_and_requires_new_intent(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    engine.set_enabled(False)
    assert engine.snapshot("led").desired.on is False
    with pytest.raises(RuntimeError):
        engine.submit(("led",), IntentPatch(on=True), source="ui")
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(5)
    assert len(transport.sent) == 1
    engine.set_enabled(True)
    await clock.advance(1)
    assert len(transport.sent) == 1
    assert engine.snapshot("led").desired.on is False


async def test_device_brightness_range_is_used_for_set_and_confirmation(rig):
    from custom_components.zigbee_led_orchestrator.light_capabilities import (
        LightCapabilities,
    )

    clock, transport, observations, engine = rig
    engine.set_capabilities("led", LightCapabilities((1, 100), (200, 400)))
    engine.submit(
        ("led",),
        IntentPatch(on=True, brightness=255, color_temp_kelvin=10000),
        source="ui",
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.7)
    assert transport.sent[-1].payload == {
        "state": "ON",
        "brightness": 100,
        "color_temp": 200,
        "transition": 0,
    }
    led(observations, engine, clock, brightness=100, kelvin=5000)
    await clock.advance(0.01)
    assert engine.snapshot("led").confirmed


async def test_unsupported_parameters_are_rejected_before_updating_intent(rig):
    from custom_components.zigbee_led_orchestrator.light_capabilities import (
        LightCapabilities,
    )

    _, _, _, engine = rig
    engine.set_capabilities("led", LightCapabilities(None, None))
    with pytest.raises(ValueError):
        engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    assert engine.snapshot("led").desired.on is False


async def test_external_on_while_observing_does_not_arm_future_control(rig):
    clock, transport, observations, engine = rig
    engine.set_enabled(False)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    assert not engine.snapshot("led").desired.on
    engine.set_enabled(True)
    await clock.advance(5)
    assert not transport.sent
    assert not engine.snapshot("led").desired.on


async def test_disabling_does_not_forget_off_in_flight(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock)
    await clock.advance(2.7)
    before = len(transport.sent)
    transport.hold()
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    assert transport.sent[-1].payload == {"state_l1": "OFF"}
    engine.set_enabled(False)
    engine.set_enabled(True)
    engine.submit(("led",), IntentPatch(on=True, brightness=123), source="ui")
    await clock.advance(0.2)
    assert len(transport.sent) == before + 1
    transport.release()
    await clock.advance(0.2)
    assert len(transport.sent) == before + 1


async def test_corrective_setting_is_not_skipped_after_unconfirmed_publish(rig):
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=100)
    await clock.advance(2.6)
    assert engine.snapshot("led").confirmed
    engine.submit(("led",), IntentPatch(brightness=200), source="ui")
    await clock.advance(0.2)
    assert transport.sent[-1].payload["brightness"] == 200
    count = len(transport.sent)
    engine.submit(("led",), IntentPatch(brightness=100), source="ui")
    await clock.advance(0.2)
    assert len(transport.sent) == count + 1
    assert transport.sent[-1].payload["brightness"] == 100
    assert not engine.snapshot("led").confirmed
