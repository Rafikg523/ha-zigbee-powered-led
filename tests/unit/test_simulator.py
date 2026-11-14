"""Deterministic test facilities for asynchronous orchestration."""

import asyncio

import pytest


async def test_clock_wakes_timers_in_deadline_order_without_real_sleep() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    events: list[tuple[str, float]] = []

    async def worker(name: str, delay: float) -> None:
        await clock.sleep(delay)
        events.append((name, clock.now()))

    early = asyncio.create_task(worker("early", 1))
    late = asyncio.create_task(worker("late", 3))
    await asyncio.sleep(0)
    await clock.advance(2)
    assert events == [("early", 1)]
    assert not late.done()
    await clock.advance(1)
    assert events == [("early", 1), ("late", 3)]
    await asyncio.gather(early, late)


async def test_clock_cancellation_and_negative_advance() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    waiter = asyncio.create_task(clock.sleep(2))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(ValueError):
        await clock.advance(-1)
    await clock.advance(3)
    assert clock.now() == 3


async def test_transport_records_copy_and_blocks_in_flight() -> None:
    from tests.helpers.clock import FakeClock
    from tests.helpers.transport import FakeTransport

    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.hold()
    payload = {"state": "ON", "nested": {"value": 1}}
    pending = asyncio.create_task(transport.publish("z2m/light/set", payload))
    await transport.wait_until_started()
    payload["state"] = "OFF"
    payload["nested"]["value"] = 2
    assert not pending.done()
    assert len(transport.sent) == 1
    assert transport.sent[0].topic == "z2m/light/set"
    assert transport.sent[0].payload == {"state": "ON", "nested": {"value": 1}}
    assert transport.sent[0].retain is False
    assert transport.sent[0].sent_at == 0
    transport.release()
    await pending


async def test_transport_failure_and_cancellation_preserve_attempt() -> None:
    from tests.helpers.clock import FakeClock
    from tests.helpers.transport import FakeTransport

    transport = FakeTransport(FakeClock())
    transport.fail_next(RuntimeError("offline"))
    with pytest.raises(RuntimeError, match="offline"):
        await transport.publish("z2m/light/set", {"state": "ON"})
    transport.hold()
    pending = asyncio.create_task(transport.publish("z2m/light/set", {"state": "OFF"}))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert [record.payload for record in transport.sent] == [
        {"state": "ON"},
        {"state": "OFF"},
    ]


async def test_clock_wakes_successive_sleeps_during_one_advance() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    events = []

    async def worker() -> None:
        await clock.sleep(1)
        events.append(clock.now())
        await clock.sleep(1)
        events.append(clock.now())

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    await clock.advance(3)
    await task
    assert events == [1, 2]
    assert clock.now() == 3


async def test_clock_wakes_timer_created_by_a_task_at_a_due_deadline() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    events = []
    spawned = []

    async def second() -> None:
        await clock.sleep(1)
        events.append(("second", clock.now()))

    async def first() -> None:
        await clock.sleep(1)
        events.append(("first", clock.now()))
        spawned.append(asyncio.create_task(second()))

    task = asyncio.create_task(first())
    await asyncio.sleep(0)
    await clock.advance(3)
    assert events == [("first", 1), ("second", 2)]
    await asyncio.gather(task, *spawned)


async def test_clock_settles_deeply_nested_spawned_tasks_before_advancing() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    events = []
    spawned = []

    async def leaf() -> None:
        await clock.sleep(1)
        events.append(clock.now())

    async def intermediary(depth: int) -> None:
        if depth:
            spawned.append(asyncio.create_task(intermediary(depth - 1)))
        else:
            spawned.append(asyncio.create_task(leaf()))

    async def first() -> None:
        await clock.sleep(1)
        spawned.append(asyncio.create_task(intermediary(8)))

    task = asyncio.create_task(first())
    await asyncio.sleep(0)
    await clock.advance(3)
    assert events == [2]
    await asyncio.gather(task, *spawned)


async def test_clock_raises_instead_of_skipping_when_loop_never_settles() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    running = True

    async def busy() -> None:
        while running:
            await asyncio.sleep(0)

    async def trigger() -> None:
        await clock.sleep(1)
        asyncio.create_task(busy())

    task = asyncio.create_task(trigger())
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="settle"):
            await clock.advance(3)
        assert clock.now() == 1
    finally:
        running = False
        await task
        await asyncio.sleep(0)


async def test_clock_rejects_real_event_loop_timer_instead_of_skipping_it() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    loop = asyncio.get_running_loop()
    handle = loop.call_later(60, lambda: None)
    try:
        with pytest.raises(RuntimeError, match="real asyncio timers"):
            await clock.advance(3)
        assert clock.now() == 0
    finally:
        handle.cancel()


async def test_clock_rejects_real_timer_created_at_deadline() -> None:
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    handles = []

    async def trigger() -> None:
        await clock.sleep(1)
        handles.append(asyncio.get_running_loop().call_later(60, lambda: None))

    task = asyncio.create_task(trigger())
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="real asyncio timers"):
            await clock.advance(3)
        assert clock.now() == 1
    finally:
        for handle in handles:
            handle.cancel()
        await task
