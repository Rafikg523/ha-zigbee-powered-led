"""Observations must never manufacture fresh evidence across power cycles."""

import importlib
import importlib.util

import pytest

from custom_components.zigbee_led_orchestrator.models import (
    BridgeObservation,
    EvidenceQuality,
    LedObservation,
    RelayObservation,
    SwitchState,
)


@pytest.fixture
def store():
    name = "custom_components.zigbee_led_orchestrator.observation_store"
    assert importlib.util.find_spec(name) is not None, "ObservationStore is missing"
    cls = importlib.import_module(name).ObservationStore
    return cls({"led1": "l1", "led2": "l2"})


def relay(channel, state, at, *, retained=False, quality=EvidenceQuality.DEVICE_REPORT):
    return RelayObservation(channel, state, at, retained, quality)


def led(light, at, *, retained=False, quality=EvidenceQuality.DEVICE_REPORT):
    return LedObservation(light, SwitchState.ON, at, retained, quality, 180, 4000)


def test_off_overrides_stale_led_on(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(led("led1", 2))
    store.ingest(relay("l1", SwitchState.OFF, 3))
    snapshot = store.snapshot("led1")
    assert snapshot.relay_state == SwitchState.OFF
    assert snapshot.led_state == SwitchState.UNKNOWN
    assert snapshot.led_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert snapshot.brightness is None
    assert snapshot.led_retained is None
    store.ingest(relay("l1", SwitchState.ON, 4))
    assert store.snapshot("led1").led_state == SwitchState.UNKNOWN


def test_retained_led_does_not_prove_readiness(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(led("led1", 2, retained=True))
    snapshot = store.snapshot("led1")
    assert snapshot.led_retained is True
    assert snapshot.led_quality == EvidenceQuality.DEVICE_REPORT
    assert snapshot.led_state == SwitchState.ON  # cached state, never readiness


def test_duplicate_on_does_not_increment_power_epoch(store):
    assert store.snapshot("led1").power_epoch == 0
    store.ingest(relay("l1", SwitchState.ON, 1))
    assert store.snapshot("led1").power_epoch == 1
    store.ingest(relay("l1", SwitchState.ON, 2))
    assert store.snapshot("led1").power_epoch == 1
    store.ingest(relay("l1", SwitchState.OFF, 3))
    store.ingest(relay("l1", SwitchState.ON, 4))
    assert store.snapshot("led1").power_epoch == 2


def test_bridge_restart_invalidates_evidence(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(led("led1", 2))
    assert store.invalidate_bridge() == 1
    snapshot = store.snapshot("led1")
    assert snapshot.bridge_epoch == 1
    assert snapshot.relay_state == SwitchState.UNKNOWN
    assert snapshot.led_state == SwitchState.UNKNOWN
    assert snapshot.led_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert snapshot.relay_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert snapshot.power_epoch == 1


def test_partial_relay_report_preserves_other_channel(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(relay("l2", SwitchState.ON, 2))
    delta = store.ingest(relay("l1", SwitchState.OFF, 3))
    assert delta.light_ids == frozenset({"led1"})
    assert store.snapshot("led2").relay_state == SwitchState.ON
    assert store.snapshot("led2").power_epoch == 1


def test_stale_delivery_does_not_undo_newer_observation(store):
    store.ingest(relay("l1", SwitchState.ON, 2))
    store.ingest(relay("l1", SwitchState.OFF, 3))
    delta = store.ingest(relay("l1", SwitchState.ON, 1))
    assert not delta.light_ids
    assert store.snapshot("led1").relay_state == SwitchState.OFF
    assert store.snapshot("led1").power_epoch == 1


def test_retained_relay_on_does_not_count_as_confirmed_power_transition(store):
    store.ingest(relay("l1", SwitchState.ON, 1, retained=True))
    snapshot = store.snapshot("led1")
    assert snapshot.relay_retained is True
    assert snapshot.relay_quality == EvidenceQuality.DEVICE_REPORT
    assert snapshot.power_epoch == 0
    store.ingest(relay("l1", SwitchState.ON, 2))
    assert store.snapshot("led1").power_epoch == 1


def test_led_report_while_off_cannot_become_new_epoch_evidence(store):
    store.ingest(relay("l1", SwitchState.OFF, 1))
    store.ingest(led("led1", 2))
    store.ingest(relay("l1", SwitchState.ON, 3))
    assert store.snapshot("led1").led_state == SwitchState.UNKNOWN


def test_bridge_offline_invalidates_once(store):
    store.ingest(BridgeObservation(True, 1, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(relay("l1", SwitchState.ON, 2))
    delta = store.ingest(
        BridgeObservation(False, 3, False, EvidenceQuality.DEVICE_REPORT)
    )
    assert delta.bridge_changed
    assert delta.light_ids == frozenset({"led1", "led2"})
    assert store.snapshot("led1").bridge_epoch == 1
    store.ingest(BridgeObservation(False, 4, False, EvidenceQuality.DEVICE_REPORT))
    assert store.snapshot("led1").bridge_epoch == 1


def test_retained_delivery_never_overwrites_live_evidence(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(relay("l1", SwitchState.ON, 2, retained=True))
    snapshot = store.snapshot("led1")
    assert snapshot.relay_state == SwitchState.ON
    assert snapshot.relay_quality == EvidenceQuality.DEVICE_REPORT
    assert snapshot.relay_retained is False


def test_unknown_ids_rejected(store):
    with pytest.raises(KeyError):
        store.ingest(relay("missing", SwitchState.ON, 1))
    with pytest.raises(KeyError):
        store.ingest(led("missing", 1))
    with pytest.raises(KeyError):
        store.snapshot("missing")


def test_report_with_receipt_time_before_new_power_epoch_is_discarded(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(led("led1", 2))
    store.ingest(relay("l1", SwitchState.OFF, 3))
    store.ingest(relay("l1", SwitchState.ON, 5))
    store.ingest(led("led1", 4))
    assert store.snapshot("led1").led_state == SwitchState.UNKNOWN


def test_reports_received_during_bridge_disconnect_do_not_confirm_power(store):
    store.ingest(BridgeObservation(True, 1, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(BridgeObservation(False, 2, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(relay("l1", SwitchState.ON, 3))
    store.ingest(BridgeObservation(True, 4, False, EvidenceQuality.DEVICE_REPORT))
    assert store.snapshot("led1").relay_state == SwitchState.UNKNOWN
    assert store.snapshot("led1").power_epoch == 0


def test_report_from_prior_bridge_session_is_discarded(store):
    store.ingest(BridgeObservation(True, 1, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(BridgeObservation(False, 4, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(BridgeObservation(True, 5, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(relay("l1", SwitchState.ON, 3))
    assert store.snapshot("led1").relay_state == SwitchState.UNKNOWN


def test_bridge_availability_is_visible_to_consumers(store):
    assert store.bridge_online is None
    store.ingest(BridgeObservation(True, 1, False, EvidenceQuality.UNKNOWN_SOURCE))
    assert store.bridge_online is True
    store.ingest(BridgeObservation(False, 2, False, EvidenceQuality.UNKNOWN_SOURCE))
    assert store.bridge_online is False


def test_bridge_observation_keeps_quality_and_invalidation_clears_availability(store):
    event = BridgeObservation(True, 1, True, EvidenceQuality.UNKNOWN_SOURCE)
    store.ingest(event)
    assert store.bridge_observation == event
    store.invalidate_bridge()
    assert store.bridge_observation is None
    assert store.bridge_online is None


def test_explicit_invalidation_waits_for_new_bridge_online(store):
    store.ingest(BridgeObservation(True, 1, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(relay("l1", SwitchState.ON, 2))
    store.invalidate_bridge()
    store.ingest(relay("l1", SwitchState.ON, 3))
    assert store.snapshot("led1").relay_state == SwitchState.UNKNOWN
    assert store.snapshot("led1").power_epoch == 1
    store.ingest(BridgeObservation(True, 4, False, EvidenceQuality.DEVICE_REPORT))
    store.ingest(relay("l1", SwitchState.ON, 5))
    assert store.snapshot("led1").power_epoch == 2


def test_equal_timestamp_off_overrides_on_conservatively(store):
    store.ingest(relay("l1", SwitchState.ON, 2))
    store.ingest(led("led1", 3))
    store.ingest(relay("l1", SwitchState.OFF, 2))
    assert store.snapshot("led1").relay_state == SwitchState.UNKNOWN
    assert store.snapshot("led1").relay_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert store.snapshot("led1").led_state == SwitchState.UNKNOWN
    store.ingest(relay("l1", SwitchState.ON, 2))
    assert store.snapshot("led1").relay_state == SwitchState.UNKNOWN


def test_conflicting_retained_off_invalidates_live_on(store):
    store.ingest(relay("l1", SwitchState.ON, 1))
    store.ingest(led("led1", 2))
    store.ingest(relay("l1", SwitchState.OFF, 3, retained=True))
    snapshot = store.snapshot("led1")
    assert snapshot.relay_state == SwitchState.UNKNOWN
    assert snapshot.relay_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert snapshot.led_state == SwitchState.UNKNOWN


def test_equal_timestamp_on_after_off_is_ambiguous(store):
    store.ingest(relay("l1", SwitchState.OFF, 2))
    store.ingest(relay("l1", SwitchState.ON, 2))
    snapshot = store.snapshot("led1")
    assert snapshot.relay_state == SwitchState.UNKNOWN
    assert snapshot.relay_quality == EvidenceQuality.UNKNOWN_SOURCE
    assert snapshot.led_state == SwitchState.UNKNOWN
    assert snapshot.power_epoch == 0
