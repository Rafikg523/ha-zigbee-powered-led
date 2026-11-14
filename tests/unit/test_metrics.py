"""Bounded counters and evidence-tagged boot samples without I/O."""

import math

import pytest

from custom_components.zigbee_led_orchestrator.desired_store import DesiredStateStore
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    PolicyConfig,
    RelayObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.observation_store import ObservationStore
from tests.helpers.clock import FakeClock
from tests.helpers.transport import FakeTransport


def test_buffer_is_bounded_and_percentiles_are_tagged_by_evidence():
    from custom_components.zigbee_led_orchestrator.metrics import Metrics

    metrics = Metrics(max_samples=100)
    for value in range(500):
        metrics.record_boot("DEVICE_REPORT", float(value))
    metrics.record_boot("CACHE", 9.0)
    boot = metrics.snapshot()["boot"]
    assert boot["DEVICE_REPORT"]["samples"] == 100
    assert boot["DEVICE_REPORT"]["p50"] == 449.0
    assert boot["DEVICE_REPORT"]["p95"] == 494.0
    assert boot["CACHE"] == {"samples": 1, "p50": 9.0, "p95": 9.0}


def test_unknown_counter_and_invalid_samples_are_rejected():
    from custom_components.zigbee_led_orchestrator.metrics import Metrics

    metrics = Metrics()
    with pytest.raises(KeyError):
        metrics.count("anything_else")
    for bad in (math.nan, math.inf, -1.0):
        with pytest.raises(ValueError):
            metrics.record_boot("DEVICE_REPORT", bad)
    assert metrics.snapshot()["boot"] == {}


@pytest.fixture
async def rig():
    from custom_components.zigbee_led_orchestrator.metrics import Metrics
    from custom_components.zigbee_led_orchestrator.reconciler import Circuit, Reconciler

    clock = FakeClock(start=1)
    transport = FakeTransport(clock)
    desired = DesiredStateStore(("led",), clock=clock)
    observations = ObservationStore({"led": "relay"})
    metrics = Metrics()
    engine = Reconciler(
        circuits=(
            Circuit(
                "led",
                "relay",
                "example/relay/set",
                "example/led/set",
                "synthetic:relay",
                "synthetic:led",
                "state_l1",
            ),
        ),
        desired=desired,
        observations=observations,
        transport=transport,
        clock=clock,
        policy=PolicyConfig(min_send_interval_ms=1, publication_timeout_ms=500),
        enabled=True,
        metrics=metrics,
    )
    observations.ingest(
        RelayObservation(
            "relay", SwitchState.OFF, 0, False, EvidenceQuality.DEVICE_REPORT
        )
    )
    yield clock, observations, engine, metrics
    await engine.close()


def report_relay(observations, engine, clock, state):
    engine.notify(
        observations.ingest(
            RelayObservation(
                "relay", state, clock.now(), False, EvidenceQuality.DEVICE_REPORT
            )
        )
    )


def report_led(observations, engine, clock):
    engine.notify(
        observations.ingest(
            LedObservation(
                "led", SwitchState.ON, clock.now(), False, EvidenceQuality.DEVICE_REPORT
            )
        )
    )


async def test_commands_and_boot_sample_come_from_real_reports(rig):
    clock, observations, engine, metrics = rig
    engine.submit(("led",), IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    report_relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    report_led(observations, engine, clock)
    await clock.advance(3)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["relay_on"] == 1
    assert snapshot["counters"]["led_set"] == 1
    assert snapshot["counters"]["commands_published"] == 2
    sample = snapshot["boot"]["DEVICE_REPORT"]
    assert sample["samples"] == 1
    assert 2.4 < sample["p50"] < 2.7


async def test_timeouts_errors_and_cancellations_are_counted(rig):
    clock, _observations, engine, metrics = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(3.1)
    counters = metrics.snapshot()["counters"]
    assert counters["timeouts"] == 1
    assert counters["errors"] == 1
    assert counters["cancellations"] == 0
    assert metrics.snapshot()["boot"] == {}


async def test_superseded_worker_is_counted_as_cancellation(rig):
    clock, _observations, engine, metrics = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    engine.submit(("led",), IntentPatch(on=False), source="ui")
    assert metrics.snapshot()["counters"]["cancellations"] == 1


async def test_stale_evidence_clears_confirmation(rig):
    clock, observations, engine, _ = rig
    engine.submit(("led",), IntentPatch(on=True), source="ui")
    await clock.advance(0.01)
    report_relay(observations, engine, clock, SwitchState.ON)
    await clock.advance(0.01)
    report_led(observations, engine, clock)
    await clock.advance(3)
    assert engine.snapshot("led").confirmed
    observations.invalidate_bridge()
    engine.notify(observations_delta(observations))
    assert not engine.snapshot("led").confirmed


def observations_delta(observations):
    from custom_components.zigbee_led_orchestrator.models import ObservationDelta

    return ObservationDelta(frozenset({"led"}))
