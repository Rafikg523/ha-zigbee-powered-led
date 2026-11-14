"""Controllable fake transport for recording asynchronous publish attempts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

from custom_components.zigbee_led_orchestrator.protocols import Clock


@dataclass(frozen=True, slots=True)
class Publication:
    topic: str
    payload: dict[str, object]
    retain: bool
    sent_at: float


class FakeTransport:
    """Record every attempt before optionally holding or failing it.

    A recorded attempt represents a command handed to transport, not a device
    acknowledgment, and remains recorded when the publishing task is cancelled.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.sent: list[Publication] = []
        self._gate = asyncio.Event()
        self._gate.set()
        self._started = asyncio.Event()
        self._next_error: Exception | None = None

    def hold(self) -> None:
        self._started.clear()
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def fail_next(self, error: Exception) -> None:
        self._next_error = error

    async def wait_until_started(self) -> None:
        await self._started.wait()

    async def publish(
        self, topic: str, payload: dict[str, object], *, retain: bool = False
    ) -> None:
        self.sent.append(
            Publication(topic, deepcopy(payload), retain, self._clock.now())
        )
        self._started.set()
        error, self._next_error = self._next_error, None
        await self._gate.wait()
        if error is not None:
            raise error
