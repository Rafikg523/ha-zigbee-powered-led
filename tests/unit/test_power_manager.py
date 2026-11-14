"""Readiness requires both fresh evidence and a minimum boot interval."""

from dataclasses import replace

from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    LightObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.power_manager import is_ready


def observation():
    return LightObservation(
        power_epoch=1,
        bridge_epoch=1,
        relay_state=SwitchState.ON,
        led_state=SwitchState.ON,
        relay_quality=EvidenceQuality.DEVICE_REPORT,
        led_quality=EvidenceQuality.DEVICE_REPORT,
        relay_received_at=10.0,
        power_started_at=10.0,
        led_received_at=11.0,
        relay_retained=False,
        led_retained=False,
    )


def test_ready_requires_boot_interval_and_current_device_report():
    item = observation()
    assert not is_ready(item, now=12.49, boot_grace_seconds=2.5)
    assert is_ready(item, now=12.5, boot_grace_seconds=2.5)


def test_cached_or_unverifiable_evidence_never_proves_readiness():
    item = observation()
    for change in (
        {"led_retained": True},
        {"led_quality": EvidenceQuality.UNKNOWN_SOURCE},
        {"relay_quality": EvidenceQuality.CACHE},
        {"relay_state": SwitchState.OFF},
        {"led_received_at": 9.0},
    ):
        assert not is_ready(replace(item, **change), now=20, boot_grace_seconds=2.5)


def test_repeated_relay_report_does_not_restart_boot_or_invalidate_led():
    from custom_components.zigbee_led_orchestrator.models import (
        LedObservation,
        RelayObservation,
    )
    from custom_components.zigbee_led_orchestrator.observation_store import (
        ObservationStore,
    )

    store = ObservationStore({"led": "relay"})
    store.ingest(
        RelayObservation(
            "relay", SwitchState.ON, 10, False, EvidenceQuality.DEVICE_REPORT
        )
    )
    store.ingest(
        LedObservation("led", SwitchState.ON, 11, False, EvidenceQuality.DEVICE_REPORT)
    )
    store.ingest(
        RelayObservation(
            "relay", SwitchState.ON, 12, False, EvidenceQuality.DEVICE_REPORT
        )
    )
    assert is_ready(store.snapshot("led"), now=12.5, boot_grace_seconds=2.5)
