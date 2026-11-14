"""Only the optional raw-Zigbee bridge can supply verified device evidence."""

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.device_reports import DeviceReports
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    SwitchState,
)
from tests.unit.test_configuration import valid_config


@pytest.fixture
def reports():
    return DeviceReports(validate_config(valid_config()))


def report(
    reports,
    seq,
    *,
    ieee="0x0000000000000001",
    endpoint=1,
    cluster="genOnOff",
    data=None,
    **changes,
):
    return {
        "session": reports.session,
        "sequence": seq,
        "ieee": ieee,
        "endpoint": endpoint,
        "cluster": cluster,
        "type": "attributeReport",
        "data": {"onOff": 1} if data is None else data,
        **changes,
    }


def test_exact_relay_endpoint_and_fresh_sequence(reports):
    events = reports.ingest(report(reports, 1), retained=False, now=1)
    assert len(events) == 1
    assert events[0].channel_id == "relay_a"
    assert events[0].state == SwitchState.ON
    assert events[0].quality == EvidenceQuality.DEVICE_REPORT
    assert not reports.ingest(report(reports, 1), retained=False, now=2)
    assert not reports.ingest(report(reports, 2, endpoint=242), retained=False, now=3)


@pytest.mark.parametrize(
    "changes,retained",
    [
        ({"session": "old-session"}, False),
        ({}, True),
        ({"type": "commandOn"}, False),
        ({"sequence": True}, False),
        ({"ieee": "0x0000000000009999"}, False),
        ({"data": {"onOff": True}}, False),
    ],
)
def test_unproven_messages_never_become_device_evidence(reports, changes, retained):
    assert not reports.ingest(report(reports, 1, **changes), retained=retained, now=1)


def test_partial_led_reports_and_power_epoch_reset(reports):
    reports.ingest(report(reports, 1), retained=False, now=1)
    led = {"ieee": "0x0000000000000002"}
    first = reports.ingest(
        report(reports, 2, **led, cluster="genLevelCtrl", data={"currentLevel": 100}),
        retained=False,
        now=2,
    )
    assert first[0].state == SwitchState.UNKNOWN
    assert first[0].brightness == 100
    second = reports.ingest(report(reports, 3, **led), retained=False, now=3)
    assert second[0].state == SwitchState.ON
    assert second[0].brightness == 100
    reports.ingest(report(reports, 4, data={"onOff": 0}), retained=False, now=4)
    assert not reports.ingest(report(reports, 5, **led), retained=False, now=5)
    reports.ingest(report(reports, 6), retained=False, now=6)
    fresh = reports.ingest(report(reports, 7, **led), retained=False, now=7)
    assert fresh[0].brightness is None


def test_session_reset_forgets_proof_and_rejects_previous_stream(reports):
    old = report(reports, 10)
    reports.ingest(old, retained=False, now=1)
    reports.reset()
    assert not reports.ingest(old, retained=False, now=2)
    assert not reports.ingest(
        report(reports, 1, ieee="0x0000000000000002"), retained=False, now=3
    )


def test_sequence_gap_revokes_stream_instead_of_carrying_old_power_proof(reports):
    reports.ingest(report(reports, 1), retained=False, now=1)
    reports.ingest(report(reports, 2, ieee="0x0000000000000002"), retained=False, now=2)
    # Sequence 3 could have been physical OFF; a later ON must not hide it.
    assert not reports.ingest(report(reports, 4), retained=False, now=4)
    assert reports.broken
    assert not reports.ready
    assert not reports.ingest(
        report(reports, 5, ieee="0x0000000000000002"), retained=False, now=5
    )


def test_new_command_discards_all_previous_led_parameter_proof(reports):
    reports.ingest(report(reports, 1), retained=False, now=1)
    led = {"ieee": "0x0000000000000002"}
    reports.ingest(report(reports, 2, **led), retained=False, now=2)
    reports.ingest(
        report(reports, 3, **led, cluster="genLevelCtrl", data={"currentLevel": 100}),
        retained=False,
        now=3,
    )
    reports.invalidate_leds(("light_a",))
    later = reports.ingest(
        report(
            reports,
            4,
            **led,
            cluster="lightingColorCtrl",
            data={"colorTemperature": 333},
        ),
        retained=False,
        now=4,
    )
    assert later[0].state == SwitchState.UNKNOWN
    assert later[0].brightness is None
