"""Recovery scenarios for physical changes and interrupted operations."""

from dataclasses import replace

import pytest

from custom_components.zigbee_led_orchestrator.models import (
    BridgeObservation,
    EvidenceQuality,
    IntentPatch,
    SwitchState,
)
from tests.unit.test_reconciler import led, relay, rig  # noqa: F401


async def test_t07_external_off_is_not_undone(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=120), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(10)
    assert engine.snapshot("led").desired.on is False
    assert engine.snapshot("led").desired.brightness == 120
    assert [item.payload for item in transport.sent] == [{"state_l1": "ON"}]


async def test_t15_reconnect_does_not_replay(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(False, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(10)
    assert len(transport.sent) == 1
    assert not engine.snapshot("led").desired.on


async def test_late_own_off_after_new_on_is_reconciled(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.5)
    assert engine.snapshot("led").desired.on
    assert [item.payload for item in transport.sent] == [
        {"state_l1": "OFF"},
        {"state_l1": "ON"},
    ]


async def test_t08_external_on_starts_new_epoch(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    assert observations.snapshot("led").power_epoch == 1
    assert engine.snapshot("led").desired.on
    await clock.advance(3)
    assert not transport.sent


async def test_fresh_same_on_after_reconnect_can_execute(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(False, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.5)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state_l1": "ON"},
    ]


async def test_disconnect_during_publish_cancels_future_work(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    transport.hold()
    engine.submit(("led",), IntentPatch(on=True, brightness=150), source="ui")
    await clock.advance(0.01)
    assert [p.payload for p in transport.sent] == [{"state_l1": "ON"}]
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(False, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    transport.release()
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(10)
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state_l1": "OFF"},
    ]
    assert not engine.snapshot("led").desired.on


async def test_t10_restart_with_relay_off_never_turns_on(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(10)
    assert not engine.snapshot("led").desired.on
    assert not transport.sent


async def test_t11_bridge_restart_clears_permission(rig):  # noqa: F811
    clock, _, observations, engine = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=99), source="ui")
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(False, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    assert not engine.snapshot("led").available
    assert not engine.snapshot("led").desired.on
    assert engine.snapshot("led").desired.brightness == 99
    with pytest.raises(RuntimeError, match="control unavailable"):
        engine.submit(("led",), IntentPatch(on=True), source="ui")


async def test_off_after_correlation_window_is_external(rig):  # noqa: F811
    clock, _, observations, engine = rig
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(3.1)
    relay(observations, engine, clock, SwitchState.OFF)
    assert not engine.snapshot("led").desired.on


async def test_late_on_after_disconnect_is_reconciled_to_off(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    transport.hold()
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(False, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    transport.release()
    await clock.advance(0.01)
    engine.notify(
        observations.ingest(
            BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
        )
    )
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.1)
    assert not engine.snapshot("led").desired.on
    assert [p.payload for p in transport.sent] == [
        {"state_l1": "ON"},
        {"state_l1": "OFF"},
    ]


async def test_external_on_restores_parameters_only_when_enabled(rig):  # noqa: F811
    clock, transport, observations, engine = rig
    engine._policy = replace(engine._policy, restore_parameters=True)
    engine.submit(("led",), IntentPatch(brightness=140), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.OFF)
    await clock.advance(0.01)
    relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    led(observations, engine, clock, brightness=50)
    await clock.advance(2.7)
    assert [p.payload for p in transport.sent] == [
        {"state": "ON", "brightness": 140, "transition": 0}
    ]
