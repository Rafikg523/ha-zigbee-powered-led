"""Store relay and LED observations without mistaking cache for evidence.

The store does not infer readiness or physical light emission. EvidenceQuality
is a claim supplied by the adapter and is preserved separately from retain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .models import (
    BridgeObservation,
    EvidenceQuality,
    LedObservation,
    LightObservation,
    Observation,
    ObservationDelta,
    RelayObservation,
    SwitchState,
)

_UNKNOWN = EvidenceQuality.UNKNOWN_SOURCE


@dataclass(slots=True)
class _LightRecord:
    snapshot: LightObservation
    confirmed_power: SwitchState = SwitchState.UNKNOWN
    # Receipt-time watermarks survive invalidation; they are not device timestamps.
    relay_watermark: float | None = None
    led_watermark: float | None = None
    power_started_at: float | None = None


def _clear_led(snapshot: LightObservation) -> LightObservation:
    return replace(
        snapshot,
        led_state=SwitchState.UNKNOWN,
        led_quality=_UNKNOWN,
        led_retained=None,
        led_received_at=None,
        brightness=None,
        color_temp_kelvin=None,
    )


def _older_or_equal(timestamp: float, watermark: float | None) -> bool:
    return watermark is not None and timestamp <= watermark


class ObservationStore:
    """Observe configured lights; the mapping connects logical IDs to channels."""

    def __init__(self, light_channels: Mapping[str, str]) -> None:
        self._records = {
            light_id: _LightRecord(
                snapshot=LightObservation(
                    power_epoch=0,
                    bridge_epoch=0,
                    relay_state=SwitchState.UNKNOWN,
                    led_state=SwitchState.UNKNOWN,
                    relay_quality=_UNKNOWN,
                    led_quality=_UNKNOWN,
                )
            )
            for light_id in light_channels
        }
        self._channels: dict[str, set[str]] = {}
        for light_id, channel_id in light_channels.items():
            self._channels.setdefault(channel_id, set()).add(light_id)
        self._bridge_epoch = 0
        self._bridge_online: bool | None = None
        self._awaiting_bridge_online = False
        self._bridge_observation: BridgeObservation | None = None
        self._bridge_watermark: float | None = None
        self._bridge_cutoff: float | None = None

    @property
    def bridge_online(self) -> bool | None:
        """Return the last bridge availability indication, or None if unknown."""
        return self._bridge_online

    @property
    def bridge_observation(self) -> BridgeObservation | None:
        """Return the last bridge report with its quality and retain flag."""
        return self._bridge_observation

    def snapshot(self, light_id: str) -> LightObservation:
        """Return an immutable point-in-time view; unknown IDs are errors."""
        return self._records[light_id].snapshot

    def invalidate_bridge(self) -> int:
        """Forget prior-session evidence without resetting power generations."""
        self._bridge_epoch += 1
        self._awaiting_bridge_online = True
        self._bridge_online = None
        self._bridge_observation = None
        for record in self._records.values():
            record.confirmed_power = SwitchState.UNKNOWN
            record.power_started_at = None
            record.snapshot = _clear_led(
                replace(
                    record.snapshot,
                    bridge_epoch=self._bridge_epoch,
                    relay_state=SwitchState.UNKNOWN,
                    relay_quality=_UNKNOWN,
                    relay_retained=None,
                    relay_received_at=None,
                )
            )
        return self._bridge_epoch

    def ingest(self, observation: Observation) -> ObservationDelta:
        """Ingest a single channel, device or bridge report and report changes."""
        if isinstance(observation, (RelayObservation, LedObservation)) and (
            self._awaiting_bridge_online
            or self._bridge_online is False
            or (
                self._bridge_cutoff is not None
                and observation.received_at <= self._bridge_cutoff
            )
        ):
            return ObservationDelta(frozenset())
        if isinstance(observation, RelayObservation):
            changed = {
                light_id
                for light_id in self._channels[observation.channel_id]
                if self._ingest_relay(self._records[light_id], observation)
            }
            return ObservationDelta(frozenset(changed))
        if isinstance(observation, LedObservation):
            record = self._records[observation.light_id]
            changed = self._ingest_led(record, observation)
            return ObservationDelta(
                frozenset({observation.light_id}) if changed else frozenset()
            )
        if isinstance(observation, BridgeObservation):
            return self._ingest_bridge(observation)
        raise TypeError("unsupported observation")

    def _ingest_relay(self, record: _LightRecord, event: RelayObservation) -> bool:
        previous = record.snapshot
        # Conflicting states with equal local receipt times have unknown order.
        # Invalidate evidence rather than presenting either state as confirmed.
        if record.relay_watermark is not None:
            if event.received_at < record.relay_watermark:
                return False
            if event.received_at == record.relay_watermark:
                if (
                    previous.relay_state == SwitchState.UNKNOWN
                    or previous.relay_state == event.state
                ):
                    return False
                # With two conflicting reports at the same local timestamp,
                # their physical order is unknown, so neither proves OFF.
                record.confirmed_power = SwitchState.UNKNOWN
                record.power_started_at = None
                record.snapshot = _clear_led(
                    replace(
                        previous,
                        relay_state=SwitchState.UNKNOWN,
                        relay_quality=_UNKNOWN,
                        relay_retained=event.retained,
                        relay_received_at=event.received_at,
                    )
                )
                return record.snapshot != previous
        # Retention does not reveal when the device state originated. An
        # opposing OFF/UNKNOWN cannot confirm OFF, but it must revoke ON proof.
        if event.retained and previous.relay_retained is False:
            if previous.relay_state == SwitchState.ON and event.state != SwitchState.ON:
                record.relay_watermark = event.received_at
                record.confirmed_power = SwitchState.UNKNOWN
                record.power_started_at = None
                record.snapshot = _clear_led(
                    replace(
                        previous,
                        relay_state=SwitchState.UNKNOWN,
                        relay_quality=_UNKNOWN,
                        relay_retained=True,
                        relay_received_at=event.received_at,
                    )
                )
                return record.snapshot != previous
            return False
        record.relay_watermark = event.received_at
        quality = event.quality
        trusted = quality == EvidenceQuality.DEVICE_REPORT and not event.retained
        current = replace(
            previous,
            relay_state=event.state,
            relay_quality=quality,
            relay_retained=event.retained,
            relay_received_at=event.received_at,
        )
        if trusted and event.state == SwitchState.ON:
            if (
                record.confirmed_power != SwitchState.ON
                or record.power_started_at is None
                or event.received_at <= record.power_started_at
            ):
                current = _clear_led(
                    replace(current, power_epoch=current.power_epoch + 1)
                )
                record.power_started_at = event.received_at
            record.confirmed_power = SwitchState.ON
        elif event.state != SwitchState.ON:
            # Even uncertain OFF/UNKNOWN invalidates readiness; only trusted OFF
            # can become confirmed evidence for a later state-machine decision.
            current = _clear_led(current)
            record.confirmed_power = SwitchState.OFF if trusted else SwitchState.UNKNOWN
            record.power_started_at = None
        record.snapshot = current
        return current != previous

    def _ingest_led(self, record: _LightRecord, event: LedObservation) -> bool:
        if _older_or_equal(event.received_at, record.led_watermark):
            return False
        previous = record.snapshot
        if event.retained and previous.led_retained is False:
            return False
        record.led_watermark = event.received_at
        # A report while power is unknown/off cannot survive to the next epoch.
        if (
            record.confirmed_power != SwitchState.ON
            or record.power_started_at is None
            or event.received_at <= record.power_started_at
        ):
            return False
        record.snapshot = replace(
            previous,
            led_state=event.state,
            led_quality=event.quality,
            led_retained=event.retained,
            led_received_at=event.received_at,
            brightness=event.brightness,
            color_temp_kelvin=event.color_temp_kelvin,
        )
        return record.snapshot != previous

    def _ingest_bridge(self, event: BridgeObservation) -> ObservationDelta:
        if _older_or_equal(event.received_at, self._bridge_watermark):
            return ObservationDelta(frozenset())
        self._bridge_watermark = event.received_at
        changed = self._bridge_online != event.online
        if changed and not event.online:
            self._bridge_cutoff = event.received_at
            self.invalidate_bridge()
        self._bridge_online = event.online
        if event.online:
            self._awaiting_bridge_online = False
        self._bridge_observation = event
        return ObservationDelta(
            frozenset(self._records) if changed else frozenset(), changed
        )
