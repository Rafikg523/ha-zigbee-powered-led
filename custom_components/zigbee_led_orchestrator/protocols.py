"""Interfaces between the independent orchestration engine and adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import Observation


class Clock(Protocol):
    def now(self) -> float:
        """Return a monotonic timestamp in seconds."""
        ...

    async def sleep(self, delay: float) -> None:
        """Wait for a monotonic duration."""
        ...


class Transport(Protocol):
    async def publish(
        self, topic: str, payload: dict[str, object], *, retain: bool = False
    ) -> None:
        """Publish a message; completion is not device confirmation."""
        ...


ObservationCallback = Callable[[Observation], None]
