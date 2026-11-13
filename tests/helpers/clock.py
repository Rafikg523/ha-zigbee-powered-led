"""Controllable monotonic clock for deterministic asynchronous tests."""

from __future__ import annotations

import asyncio
import heapq
import math
from itertools import count


class FakeClock:
    """Wake sleepers in deadline order and drain runnable asyncio callbacks.

    This deterministic fake supports the standard CPython asyncio event loop and
    in-memory tasks only; real I/O and real asyncio timers are outside its model.
    It inspects the loop's private ready queue (not a portable asyncio API).
    Unsupported loop implementations and non-quiescing work fail explicitly.
    Every settling phase is limited to 1000 cooperative event-loop turns; after
    exhaustion, time remains at the last deadline and RuntimeError is raised.
    """

    def __init__(self, start: float = 0.0) -> None:
        if not math.isfinite(start):
            raise ValueError("start must be finite")
        self._now = float(start)
        self._sequence = count()
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, delay: float) -> None:
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("delay must be finite and nonnegative")
        if delay == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._now + delay, next(self._sequence), future))
        await future

    @staticmethod
    async def _settle() -> None:
        """Drain ready callbacks, including tasks created by other tasks."""
        loop = asyncio.get_running_loop()
        ready = getattr(loop, "_ready", None)
        scheduled = getattr(loop, "_scheduled", None)
        if ready is None or scheduled is None:
            raise RuntimeError("FakeClock cannot settle this event loop")
        # Yield at least once so tasks queued before advance can register sleeps.
        for _ in range(1000):
            if any(not handle.cancelled() for handle in scheduled):
                raise RuntimeError("FakeClock cannot settle real asyncio timers")
            await asyncio.sleep(0)
            if any(not handle.cancelled() for handle in scheduled):
                raise RuntimeError("FakeClock cannot settle real asyncio timers")
            if not ready:
                return
        raise RuntimeError("FakeClock could not settle the event loop in 1000 turns")

    async def advance(self, seconds: float) -> None:
        """Advance time and allow tasks due at each deadline to run."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be finite and nonnegative")
        target = self._now + seconds
        await self._settle()
        while self._waiters and self._waiters[0][0] <= target:
            deadline = self._waiters[0][0]
            self._now = deadline
            while self._waiters and self._waiters[0][0] == deadline:
                _, _, future = heapq.heappop(self._waiters)
                if not future.done():
                    future.set_result(None)
            await self._settle()
        self._now = target
