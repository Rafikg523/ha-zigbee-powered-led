"""Conservative readiness checks for a relay-powered LED controller."""

from __future__ import annotations

import math

from .models import EvidenceQuality, LightObservation, SwitchState


def is_ready(
    observation: LightObservation, *, now: float, boot_grace_seconds: float
) -> bool:
    """Require current, verified device reports and the minimum boot interval.

    DEVICE_REPORT must be assigned by an adapter with evidence of its source;
    receipt time or a non-retained message alone is not such evidence.
    """
    if not math.isfinite(now) or not math.isfinite(boot_grace_seconds):
        raise ValueError("times must be finite")
    if boot_grace_seconds < 0:
        raise ValueError("boot_grace_seconds must not be negative")
    relay_at = observation.relay_received_at
    led_at = observation.led_received_at
    return bool(
        observation.power_epoch > 0
        and observation.relay_state == SwitchState.ON
        and observation.relay_quality == EvidenceQuality.DEVICE_REPORT
        and observation.led_quality == EvidenceQuality.DEVICE_REPORT
        and observation.relay_retained is False
        and observation.led_retained is False
        and relay_at is not None
        and led_at is not None
        and relay_at < led_at <= now
        and now - relay_at >= boot_grace_seconds
    )
