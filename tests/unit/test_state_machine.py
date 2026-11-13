"""State transitions keep transport submission distinct from device evidence."""

import pytest

from custom_components.zigbee_led_orchestrator.models import Phase
from custom_components.zigbee_led_orchestrator.state_machine import Event, StateMachine


def test_on_progression_requires_confirmation_after_publish():
    machine = StateMachine()
    assert machine.phase == Phase.OFF
    assert machine.advance(Event.POWER_REQUESTED) == Phase.POWERING
    assert machine.advance(Event.POWER_CONFIRMED) == Phase.WAITING_READY
    assert machine.advance(Event.READY) == Phase.APPLYING
    assert machine.advance(Event.SET_SENT) == Phase.UNCONFIRMED
    assert machine.advance(Event.LED_CONFIRMED) == Phase.ON


def test_off_interrupts_startup_and_late_ready_is_rejected():
    machine = StateMachine()
    machine.advance(Event.POWER_REQUESTED)
    machine.advance(Event.POWER_CONFIRMED)
    assert machine.advance(Event.OFF_REQUESTED) == Phase.APPLYING
    with pytest.raises(ValueError):
        machine.advance(Event.READY)
    with pytest.raises(ValueError):
        machine.advance(Event.LED_CONFIRMED)
    assert machine.advance(Event.OFF_CONFIRMED) == Phase.OFF


def test_timeout_is_error_and_explicit_request_can_retry():
    machine = StateMachine()
    machine.advance(Event.POWER_REQUESTED)
    assert machine.advance(Event.FAILED) == Phase.ERROR
    assert machine.advance(Event.POWER_REQUESTED) == Phase.POWERING


def test_new_on_can_supersede_pending_off_without_accepting_stale_off():
    machine = StateMachine()
    machine.advance(Event.POWER_REQUESTED)
    machine.advance(Event.OFF_REQUESTED)
    assert machine.advance(Event.POWER_REQUESTED) == Phase.POWERING
    with pytest.raises(ValueError):
        machine.advance(Event.OFF_CONFIRMED)


def test_fresh_matching_report_can_confirm_without_a_redundant_set():
    machine = StateMachine()
    machine.advance(Event.POWER_REQUESTED)
    machine.advance(Event.POWER_CONFIRMED)
    machine.advance(Event.READY)
    assert machine.advance(Event.LED_CONFIRMED) == Phase.ON
