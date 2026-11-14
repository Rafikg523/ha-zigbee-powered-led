"""Pure phase transitions for a single relay-powered light."""

from enum import StrEnum

from .models import Phase


class Event(StrEnum):
    POWER_REQUESTED = "power_requested"
    POWER_CONFIRMED = "power_confirmed"
    READY = "ready"
    SET_SENT = "set_sent"
    LED_CONFIRMED = "led_confirmed"
    OFF_REQUESTED = "off_requested"
    OFF_CONFIRMED = "off_confirmed"
    FAILED = "failed"


class StateMachine:
    """Track phase only; callers validate generation and evidence separately."""

    def __init__(self) -> None:
        self._phase = Phase.OFF
        self._turning_off = False

    @property
    def phase(self) -> Phase:
        return self._phase

    def advance(self, event: Event) -> Phase:
        """Apply a valid event; late or incompatible events change nothing."""
        current = self._phase
        if event == Event.OFF_REQUESTED:
            self._turning_off = True
            self._phase = Phase.APPLYING
        elif event == Event.FAILED and current != Phase.OFF:
            self._phase = Phase.ERROR
        elif event == Event.POWER_REQUESTED:
            self._turning_off = False
            self._phase = Phase.POWERING
        elif event == Event.POWER_CONFIRMED and current == Phase.POWERING:
            self._phase = Phase.WAITING_READY
        elif event == Event.READY and current == Phase.WAITING_READY:
            self._phase = Phase.APPLYING
        elif (
            event == Event.SET_SENT
            and current == Phase.APPLYING
            and not self._turning_off
        ):
            self._phase = Phase.UNCONFIRMED
        elif (
            event == Event.LED_CONFIRMED
            and current in (Phase.APPLYING, Phase.UNCONFIRMED)
            and not self._turning_off
        ):
            self._phase = Phase.ON
        elif event == Event.OFF_CONFIRMED and self._turning_off:
            self._turning_off = False
            self._phase = Phase.OFF
        else:
            raise ValueError(f"invalid event {event} in phase {current}")
        return self._phase
