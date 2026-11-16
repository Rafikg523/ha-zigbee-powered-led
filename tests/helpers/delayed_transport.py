"""MQTT accepts immediately; Zigbee executes a FIFO per physical address."""

import asyncio
from collections import defaultdict, deque

from tests.helpers.transport import FakeTransport, Publication


class DelayedDeviceTransport(FakeTransport):
    def __init__(self, clock, *, route, execute, delay=3.0):
        super().__init__(clock)
        self.route, self.execute, self.delay = route, execute, delay
        self.executed = []
        self.queues = defaultdict(deque)
        self.tasks = {}
        self.maximum_depth = defaultdict(int)

    async def publish(self, topic, payload, *, retain=False):
        await super().publish(topic, payload, retain=retain)
        device = self.route(topic)
        self.queues[device].append(self.sent[-1])
        self.maximum_depth[device] = max(
            self.maximum_depth[device], len(self.queues[device])
        )
        if device not in self.tasks or self.tasks[device].done():
            self.tasks[device] = asyncio.create_task(self._run(device))

    async def _run(self, device):
        while self.queues[device]:
            await self._clock.sleep(self.delay)
            publication = self.queues[device].popleft()
            self.executed.append(
                Publication(
                    publication.topic, publication.payload, False, self._clock.now()
                )
            )
            self.execute(publication)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
