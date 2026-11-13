"""Behavior of the latest-intent store without Home Assistant."""

import pytest

from custom_components.zigbee_led_orchestrator.models import IntentPatch


def test_ten_intents_keep_only_latest() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    store = DesiredStateStore(("one",), clock=clock)
    previous = store.get("one").generation
    for index in range(10):
        state = store.apply(("one",), IntentPatch(on=index % 2 == 0), source="ui")[
            "one"
        ]
        assert state.generation > previous
        previous = state.generation
    assert store.get("one").on is False
    assert store.get("one").generation == 10


def test_batch_has_one_generation() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one", "two"), clock=FakeClock())
    states = store.apply(("one", "two"), IntentPatch(on=True), source="group")
    assert states["one"].on is states["two"].on is True
    assert states["one"].generation == states["two"].generation == 1


def test_off_preserves_parameters() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one",), clock=FakeClock())
    store.apply(
        ("one",),
        IntentPatch(on=True, brightness=150, color_temp_kelvin=4000),
        source="ui",
    )
    result = store.apply(("one",), IntentPatch(on=False), source="ui")["one"]
    assert (result.on, result.brightness, result.color_temp_kelvin) == (
        False,
        150,
        4000,
    )
    result = store.apply(("one",), IntentPatch(brightness=0), source="ui")["one"]
    assert (result.on, result.brightness, result.color_temp_kelvin) == (
        False,
        150,
        4000,
    )


def test_duplicate_noop_does_not_enqueue_work() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    clock = FakeClock()
    store = DesiredStateStore(("one",), clock=clock)
    first = store.apply(("one",), IntentPatch(on=True), source="ui")["one"]
    clock_now = first.updated_at
    second = store.apply(("one",), IntentPatch(on=True), source="automation")["one"]
    assert second == first
    assert second.updated_at == clock_now


def test_unknown_member_is_atomic() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one",), clock=FakeClock())
    with pytest.raises(KeyError):
        store.apply(("one", "missing"), IntentPatch(on=True), source="group")
    assert store.get("one").on is False
    assert store.get("one").generation == 0


def test_batch_updates_changed_members_only() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one", "two"), clock=FakeClock())
    initial = store.apply(("one",), IntentPatch(on=True), source="ui")["one"]
    result = store.apply(("one", "two"), IntentPatch(on=True), source="group")
    assert result["one"] == initial
    assert result["two"].generation == 2


def test_parameters_alone_do_not_turn_light_on() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one",), clock=FakeClock())
    result = store.apply(("one",), IntentPatch(brightness=180), source="ui")["one"]
    assert result.on is False
    assert result.brightness == 180


async def test_update_uses_monotonic_clock_without_retimestamping_noop() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    clock = FakeClock(start=100)
    store = DesiredStateStore(("one",), clock=clock)
    await clock.advance(2.5)
    first = store.apply(("one",), IntentPatch(on=True), source="ui")["one"]
    await clock.advance(1)
    unchanged = store.apply(("one",), IntentPatch(on=True), source="automation")["one"]
    assert first.updated_at == unchanged.updated_at == 102.5


def test_batch_rejects_bad_patch_or_source_without_partial_mutation() -> None:
    from custom_components.zigbee_led_orchestrator.desired_store import (
        DesiredStateStore,
    )
    from tests.helpers.clock import FakeClock

    store = DesiredStateStore(("one", "two"), clock=FakeClock())
    with pytest.raises(ValueError):
        store.apply(("one", "two"), IntentPatch(on=True), source="  ")
    with pytest.raises(TypeError):
        store.apply(("one", "two"), object(), source="ui")
    assert not store.get("one").on and not store.get("two").on
    assert store.get("one").generation == store.get("two").generation == 0
