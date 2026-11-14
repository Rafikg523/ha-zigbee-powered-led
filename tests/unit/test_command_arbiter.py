"""Behavioral checks for priority, freshness and concurrent MQTT sends."""

import asyncio

import pytest

from custom_components.zigbee_led_orchestrator.command_arbiter import CommandArbiter
from custom_components.zigbee_led_orchestrator.models import (
    CommandKind,
    CommandPriority,
    CommandToken,
    SendRequest,
    SendStatus,
)
from tests.helpers.clock import FakeClock
from tests.helpers.transport import FakeTransport


def request(name, resource, *, kind=CommandKind.RELAY_ON, generation=1):
    return SendRequest(
        topic=f"z2m/{name}/set",
        payload={"state": "OFF" if kind == CommandKind.RELAY_OFF else "ON"},
        tokens=(CommandToken(resource, generation, 1, 1, 1),),
        resources=(resource,),
        priority=CommandPriority.OFF
        if kind == CommandKind.RELAY_OFF
        else CommandPriority.NORMAL,
        operation_id=name,
        kind=kind,
        topology_version=1,
    )


@pytest.fixture
def setup():
    clock = FakeClock()
    transport = FakeTransport(clock)
    current = {"value": True}
    arbiter = CommandArbiter(
        transport,
        clock,
        is_current=lambda req: current["value"],
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    return clock, transport, current, arbiter


async def test_stale_after_rate_wait_is_not_sent(setup):
    clock, transport, current, arbiter = setup
    assert (await arbiter.send(request("first", "a"))).status == SendStatus.SENT
    waiting = asyncio.create_task(arbiter.send(request("second", "b")))
    await clock.advance(0)
    current["value"] = False
    await clock.advance(0.1)
    assert (await waiting).status == SendStatus.STALE
    assert len(transport.sent) == 1
    await arbiter.close()


async def test_off_bypasses_slider_debounce(setup):
    clock, transport, _, arbiter = setup
    slider = asyncio.create_task(
        arbiter.send(request("slider", "a", kind=CommandKind.LED_SET))
    )
    await clock.advance(0.01)
    off = asyncio.create_task(
        arbiter.send(request("off", "a", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(0)
    assert (await slider).status == SendStatus.STALE
    assert (await off).status == SendStatus.SENT
    assert [x.topic for x in transport.sent] == ["z2m/off/set"]
    assert transport.sent[0].retain is False
    await arbiter.close()


async def test_independent_resources_progress(setup):
    clock, transport, _, arbiter = setup
    transport.hold()
    first = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(0)
    second = asyncio.create_task(arbiter.send(request("second", "b")))
    await clock.advance(0.1)
    assert [x.topic for x in transport.sent] == ["z2m/first/set", "z2m/second/set"]
    transport.release()
    await clock.advance(0)
    assert (await first).status == (await second).status == SendStatus.SENT
    await arbiter.close()


async def test_conflicting_resources_never_publish_concurrently(setup):
    clock, transport, _, arbiter = setup
    transport.hold()
    first = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(0)
    second = asyncio.create_task(arbiter.send(request("second", "a")))
    await clock.advance(0.2)
    assert len(transport.sent) == 1
    transport.release()
    await clock.advance(0)
    assert len(transport.sent) == 2
    assert (await first).status == (await second).status == SendStatus.SENT
    await arbiter.close()


async def test_close_during_wait_prevents_publish(setup):
    clock, transport, _, arbiter = setup
    assert (await arbiter.send(request("first", "a"))).status == SendStatus.SENT
    waiting = asyncio.create_task(arbiter.send(request("second", "b")))
    await clock.advance(0)
    await arbiter.close()
    assert (await waiting).status == SendStatus.BLOCKED
    await clock.advance(1)
    assert len(transport.sent) == 1


async def test_publish_timeout_reports_unknown_delivery(setup):
    clock, transport, _, arbiter = setup
    transport.hold()
    task = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(1)
    result = await task
    assert result.status == SendStatus.FAILED
    assert result.reason == "delivery_unknown"
    assert len(transport.sent) == 1
    await arbiter.close()


async def test_mode_epoch_invalidates_waiting_command(setup):
    clock, transport, current, arbiter = setup
    pending = asyncio.create_task(
        arbiter.send(request("slider", "a", kind=CommandKind.LED_SET))
    )
    await clock.advance(0)
    current["value"] = False
    await clock.advance(0.12)
    assert (await pending).status == SendStatus.STALE
    assert not transport.sent
    await arbiter.close()


async def test_waiting_off_precedes_waiting_on(setup):
    clock, transport, _, arbiter = setup
    assert (await arbiter.send(request("first", "a"))).status == SendStatus.SENT
    older = asyncio.create_task(arbiter.send(request("older", "b")))
    await clock.advance(0)
    off = asyncio.create_task(
        arbiter.send(request("off", "c", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(0.1)
    assert (await off).status == SendStatus.SENT
    assert [p.topic for p in transport.sent] == ["z2m/first/set", "z2m/off/set"]
    await clock.advance(0.1)
    assert (await older).status == SendStatus.SENT
    await arbiter.close()


async def test_coalesces_overlapping_slider_settings(setup):
    clock, transport, _, arbiter = setup
    old = asyncio.create_task(
        arbiter.send(request("old", "a", kind=CommandKind.LED_SET))
    )
    await clock.advance(0.06)
    new = asyncio.create_task(
        arbiter.send(request("new", "a", kind=CommandKind.LED_SET))
    )
    await clock.advance(0.119)
    assert (await old).status == SendStatus.STALE
    assert not transport.sent
    await clock.advance(0.001)
    assert (await new).status == SendStatus.SENT
    assert [p.topic for p in transport.sent] == ["z2m/new/set"]
    await arbiter.close()


async def test_close_during_publish_does_not_claim_unsent(setup):
    clock, transport, _, arbiter = setup
    transport.hold()
    task = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(0)
    await arbiter.close()
    result = await task
    assert result.status == SendStatus.FAILED
    assert result.reason == "delivery_unknown"
    assert len(transport.sent) == 1


async def test_failed_publish_is_not_retried(setup):
    clock, transport, _, arbiter = setup
    transport.fail_next(OSError("offline"))
    result = await arbiter.send(request("first", "a"))
    assert result.status == SendStatus.FAILED
    await clock.advance(1)
    assert len(transport.sent) == 1
    await arbiter.close()


async def test_stale_while_waiting_for_resource_does_not_publish(setup):
    clock, transport, current, arbiter = setup
    transport.hold()
    first = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(0)
    waiting = asyncio.create_task(arbiter.send(request("waiting", "a")))
    await clock.advance(0.2)
    current["value"] = False
    transport.release()
    await clock.advance(0)
    assert (await waiting).status == SendStatus.STALE
    assert (await first).status == SendStatus.SENT
    assert len(transport.sent) == 1
    await arbiter.close()


async def test_cancelling_queued_send_prevents_publish(setup):
    clock, transport, _, arbiter = setup
    assert (await arbiter.send(request("first", "a"))).status == SendStatus.SENT
    waiting = asyncio.create_task(arbiter.send(request("waiting", "b")))
    await clock.advance(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await clock.advance(0.2)
    assert len(transport.sent) == 1
    await arbiter.close()


async def test_close_at_every_dispatch_turn_completes_sender():
    for turns in range(8):
        clock = FakeClock()
        transport = FakeTransport(clock)
        transport.hold()
        arbiter = CommandArbiter(
            transport,
            clock,
            is_current=lambda _: True,
            min_send_interval_ms=100,
            publication_timeout_ms=1000,
            slider_coalesce_ms=120,
        )
        sender = asyncio.create_task(arbiter.send(request("first", "a")))
        for _ in range(turns):
            await asyncio.sleep(0)
        await arbiter.close()
        for _ in range(10):
            await asyncio.sleep(0)
        assert sender.done(), f"sender stuck after close at turn {turns}"
        result = await sender
        assert result.status in (SendStatus.BLOCKED, SendStatus.FAILED)


async def test_cancel_at_every_dispatch_turn_does_not_stall_queue():
    clock = FakeClock()
    for turns in range(8):
        transport = FakeTransport(clock)
        transport.hold()
        arbiter = CommandArbiter(
            transport,
            clock,
            is_current=lambda _: True,
            min_send_interval_ms=100,
            publication_timeout_ms=1000,
            slider_coalesce_ms=120,
        )
        first = asyncio.create_task(arbiter.send(request("first", "a")))
        for _ in range(turns):
            await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        transport.release()
        second = asyncio.create_task(arbiter.send(request("second", "a")))
        await clock.advance(0.2)
        assert second.done(), f"second sender stuck after cancellation at turn {turns}"
        assert (await second).status == SendStatus.SENT
        await arbiter.close()


async def test_validation_exception_fails_closed_without_stalling_later_commands():
    clock = FakeClock()
    transport = FakeTransport(clock)
    state = {"raises": False}

    def is_current(_):
        if state["raises"]:
            raise RuntimeError("topology unavailable")
        return True

    arbiter = CommandArbiter(
        transport,
        clock,
        is_current=is_current,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    assert (await arbiter.send(request("first", "a"))).status == SendStatus.SENT
    waiting = asyncio.create_task(arbiter.send(request("waiting", "b")))
    await clock.advance(0)
    state["raises"] = True
    await clock.advance(0.1)
    assert waiting.done()
    assert (await waiting).status == SendStatus.STALE
    state["raises"] = False
    assert (await arbiter.send(request("later", "b"))).status == SendStatus.SENT
    assert len(transport.sent) == 2
    await arbiter.close()


async def test_uncooperative_publish_is_quarantined_and_close_returns():
    class StubbornTransport(FakeTransport):
        async def publish(self, topic, payload, *, retain=False):
            try:
                await super().publish(topic, payload, retain=retain)
            except asyncio.CancelledError:
                await self._gate.wait()

    clock = FakeClock()
    transport = StubbornTransport(clock)
    transport.hold()
    arbiter = CommandArbiter(
        transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    first = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(1)
    assert first.done()
    assert (await first).status == SendStatus.FAILED
    assert (await first).reason == "delivery_unknown"
    off = asyncio.create_task(
        arbiter.send(request("off", "a", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(0)
    assert off.done()
    assert (await off).status == SendStatus.BLOCKED
    assert len(transport.sent) == 1
    await arbiter.close()
    transport.release()
    await clock.advance(0)


async def test_duplicate_relay_on_is_not_republished_but_new_token_is_allowed(setup):
    clock, transport, _, arbiter = setup
    transport.hold()
    first_request = request("same", "a")

    def duplicate(operation_id, generation):
        return SendRequest(
            topic=first_request.topic,
            payload=first_request.payload,
            tokens=(CommandToken("a", generation, 1, 1, 1),),
            resources=("a",),
            priority=CommandPriority.NORMAL,
            operation_id=operation_id,
            kind=CommandKind.RELAY_ON,
            topology_version=1,
        )

    first = asyncio.create_task(arbiter.send(first_request))
    await clock.advance(0)
    two = await arbiter.send(duplicate("another-id", 1))
    three = await arbiter.send(duplicate("third-id", 1))
    assert two.status == three.status == SendStatus.BLOCKED
    assert two.reason == three.reason == "duplicate"
    newer = asyncio.create_task(arbiter.send(duplicate("new-generation", 2)))
    await clock.advance(0.2)
    assert len(transport.sent) == 1
    transport.release()
    await clock.advance(0)
    assert (await first).status == (await newer).status == SendStatus.SENT
    assert len(transport.sent) == 2
    await arbiter.close()


async def test_quarantine_blocks_queued_work_without_blocking_other_resources():
    class StubbornTransport(FakeTransport):
        async def publish(self, topic, payload, *, retain=False):
            if topic.endswith("first/set"):
                try:
                    await super().publish(topic, payload, retain=retain)
                except asyncio.CancelledError:
                    await self._gate.wait()
            else:
                self.sent.append((topic, payload, retain))

    clock = FakeClock()
    transport = StubbornTransport(clock)
    transport.hold()
    arbiter = CommandArbiter(
        transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    first = asyncio.create_task(arbiter.send(request("first", "a")))
    await clock.advance(0)
    off = asyncio.create_task(
        arbiter.send(request("off", "a", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(1)
    assert (await first).reason == "delivery_unknown"
    assert (await off).status == SendStatus.BLOCKED
    independent = asyncio.create_task(arbiter.send(request("other", "b")))
    await clock.advance(0)
    assert (await independent).status == SendStatus.SENT
    transport.release()
    await clock.advance(0)
    assert len(transport.sent) == 2
    renewed = asyncio.create_task(arbiter.send(request("renewed", "a")))
    await clock.advance(0.1)
    assert (await renewed).status == SendStatus.SENT
    await arbiter.close()


async def test_reload_cannot_bypass_quarantine_for_late_publish():
    class StubbornTransport(FakeTransport):
        async def publish(self, topic, payload, *, retain=False):
            try:
                await super().publish(topic, payload, retain=retain)
            except asyncio.CancelledError:
                await self._gate.wait()

    clock = FakeClock()
    old_transport = StubbornTransport(clock)
    old_transport.hold()
    old = CommandArbiter(
        old_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    on = asyncio.create_task(old.send(request("on", "same-installation-relay")))
    await clock.advance(1)
    assert (await on).reason == "delivery_unknown"
    await old.close()
    new_transport = FakeTransport(clock)
    new = CommandArbiter(
        new_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    off = await new.send(
        request("off", "same-installation-relay", kind=CommandKind.RELAY_OFF)
    )
    assert off.status == SendStatus.BLOCKED
    assert not new_transport.sent
    old_transport.release()
    await clock.advance(0)
    off = asyncio.create_task(
        new.send(request("off2", "same-installation-relay", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(0)
    assert (await off).status == SendStatus.SENT
    await new.close()


async def test_second_active_arbiter_is_rejected_until_first_closes():
    clock = FakeClock()
    first_transport, second_transport = FakeTransport(clock), FakeTransport(clock)
    first = CommandArbiter(
        first_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    second = CommandArbiter(
        second_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    assert (await first.send(request("on", "a"))).status == SendStatus.SENT
    result = await second.send(request("off", "b", kind=CommandKind.RELAY_OFF))
    assert (result.status, result.reason) == (
        SendStatus.BLOCKED,
        "arbiter_already_active",
    )
    assert not second_transport.sent
    await first.close()
    pending = asyncio.create_task(
        second.send(request("off", "b", kind=CommandKind.RELAY_OFF))
    )
    await clock.advance(0.1)
    assert (await pending).status == SendStatus.SENT
    await second.close()


async def test_rate_limit_survives_arbiter_reload():
    clock = FakeClock()
    first_transport, second_transport = FakeTransport(clock), FakeTransport(clock)
    first = CommandArbiter(
        first_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    assert (await first.send(request("first", "a"))).status == SendStatus.SENT
    await first.close()
    second = CommandArbiter(
        second_transport,
        clock,
        is_current=lambda _: True,
        min_send_interval_ms=100,
        publication_timeout_ms=1000,
        slider_coalesce_ms=120,
    )
    pending = asyncio.create_task(second.send(request("second", "b")))
    await clock.advance(0)
    assert not second_transport.sent
    await clock.advance(0.1)
    assert (await pending).status == SendStatus.SENT
    await second.close()
