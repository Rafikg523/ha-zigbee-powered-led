"""Bounded operational counters and evidence-tagged boot samples."""

from __future__ import annotations

import math
from collections import deque
from copy import deepcopy
from math import ceil

COUNTERS = (
    "commands_published",
    "relay_on",
    "relay_off",
    "led_set",
    "led_off",
    "group_set",
    "probes",
    "timeouts",
    "errors",
    "conflicts",
    "cancellations",
)


class Metrics:
    """Keep fixed counter names and a bounded buffer of samples per evidence type.

    Boot samples must be measured from device reports, not from MQTT publication;
    callers tag each sample with the evidence quality that ended the measurement.
    """

    def __init__(self, max_samples: int = 100) -> None:
        if type(max_samples) is not int or max_samples <= 0:
            raise ValueError("max_samples must be a positive integer")
        self._max = max_samples
        self._counters = dict.fromkeys(COUNTERS, 0)
        self._boot: dict[str, deque[float]] = {}
        self._events: deque[dict] = deque(maxlen=1024)
        self._events_dropped = 0

    def trace(self, event: dict) -> None:
        if len(self._events) == self._events.maxlen:
            self._events_dropped += 1
        self._events.append(deepcopy(event))

    def count(self, name: str, amount: int = 1) -> None:
        if name not in self._counters:
            raise KeyError(name)
        self._counters[name] += amount

    def record_boot(self, evidence: str, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("boot sample must be finite and nonnegative")
        self._boot.setdefault(evidence, deque(maxlen=self._max)).append(seconds)

    @staticmethod
    def _percentile(ordered: list[float], percent: int) -> float:
        return ordered[ceil(percent / 100 * len(ordered)) - 1]

    def snapshot(self) -> dict[str, object]:
        boot = {}
        for evidence, samples in self._boot.items():
            ordered = sorted(samples)
            boot[evidence] = {
                "samples": len(ordered),
                "p50": self._percentile(ordered, 50),
                "p95": self._percentile(ordered, 95),
            }
        return {
            "counters": dict(self._counters),
            "boot": boot,
            "events": deepcopy(list(self._events)),
            "events_dropped": self._events_dropped,
        }
