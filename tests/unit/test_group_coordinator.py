"""Group barrier behavior with two synthetic circuits and simulated time."""

import pytest

from custom_components.zigbee_led_orchestrator.desired_store import DesiredStateStore
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    Phase,
    PolicyConfig,
    RelayObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.observation_store import ObservationStore
from tests.helpers.clock import FakeClock
from tests.helpers.transport import FakeTransport

GROUP_TOPIC = "example/group/set"
LIGHTS = ("a", "b")


@pytest.fixture
async def rig(request):
    from custom_components.zigbee_led_orchestrator.group_coordinator import (
        GroupCoordinator,
    )
    from custom_components.zigbee_led_orchestrator.reconciler import (
        Circuit,
        GroupCircuit,
        Reconciler,
    )

    clock = FakeClock(start=1)
    transport = FakeTransport(clock)
    desired = DesiredStateStore(LIGHTS, clock=clock)
    observations = ObservationStore({item: f"relay_{item}" for item in LIGHTS})
    circuits = tuple(
        Circuit(
            light_id=item,
            relay_channel_id=f"relay_{item}",
            relay_topic="example/relay/set",
            led_topic=f"example/led_{item}/set",
            relay_resource=f"synthetic:relay:channel_{item}",
            led_resource=f"synthetic:led_{item}:endpoint1",
            relay_state_key=f"state_{item}",
        )
        for item in LIGHTS
    )
    policy = PolicyConfig(min_send_interval_ms=1, publication_timeout_ms=500)
    engine = Reconciler(
        circuits=circuits,
        desired=desired,
        observations=observations,
        transport=transport,
        clock=clock,
        policy=policy,
        enabled=True,
        groups=(
            GroupCircuit(
                group_id="group",
                topic=GROUP_TOPIC,
                resource="synthetic:group:1",
                members=LIGHTS,
            ),
        ),
    )
    for item in LIGHTS:
        observations.ingest(
            RelayObservation(
                f"relay_{item}",
                SwitchState.OFF,
                0,
                False,
                EvidenceQuality.DEVICE_REPORT,
            )
        )
    coordinator = GroupCoordinator(
        engine,
        clock,
        barrier_ms=policy.group_barrier_timeout_ms,
        strategy=getattr(request, "param", "individual"),
    )
    yield clock, transport, observations, engine, coordinator
    await coordinator.close()
    await engine.close()


def relay(observations, engine, clock, item, state=SwitchState.ON):
    engine.notify(
        observations.ingest(
            RelayObservation(
                f"relay_{item}",
                state,
                clock.now(),
                False,
                EvidenceQuality.DEVICE_REPORT,
            )
        )
    )


def led(observations, engine, clock, item, *, brightness=None, kelvin=None):
    engine.notify(
        observations.ingest(
            LedObservation(
                item,
                SwitchState.ON,
                clock.now(),
                False,
                EvidenceQuality.DEVICE_REPORT,
                brightness=brightness,
                color_temp_kelvin=kelvin,
            )
        )
    )


def topics(transport):
    return [x.topic for x in transport.sent]


@pytest.mark.parametrize("rig", ["individual", "wait_all"], indirect=True)
async def test_late_member_finishes_group_goal_in_selected_strategy(rig, request):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "a")
    relay(observations, engine, clock, "b")
    await clock.advance(0.01)
    led(observations, engine, clock, "a", brightness=20)
    await clock.advance(12)
    before = [x.topic for x in transport.sent if "relay" not in x.topic]
    led(observations, engine, clock, "b", brightness=20)
    await clock.advance(0.3)
    after = [x.topic for x in transport.sent if "relay" not in x.topic]
    if request.node.callspec.params["rig"] == "wait_all":
        assert before == []
        assert after == [GROUP_TOPIC]
    else:
        assert before == ["example/led_a/set"]
        assert after == ["example/led_a/set", "example/led_b/set"]


@pytest.mark.parametrize("rig", ["individual", "wait_all"], indirect=True)
@pytest.mark.parametrize("stop", ["off", "physical_off", "topology", "close"])
async def test_late_group_wait_is_revoked_before_any_late_send(rig, stop):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    for item in LIGHTS:
        relay(observations, engine, clock, item)
    await clock.advance(12)
    if stop == "off":
        coordinator.submit("group", IntentPatch(on=False), source="ui")
    elif stop == "physical_off":
        for item in LIGHTS:
            relay(observations, engine, clock, item, SwitchState.OFF)
    elif stop == "topology":
        engine.topology_changed(2)
    else:
        await coordinator.close()
        await engine.close()
        return
    await clock.advance(0.01)
    for item in LIGHTS:
        led(observations, engine, clock, item, brightness=20)
    await clock.advance(60)
    assert GROUP_TOPIC not in topics(transport)
    assert not [x for x in topics(transport) if x.startswith("example/led_")]


async def power_both(clock, observations, engine, *, only=LIGHTS):
    await clock.advance(0.01)
    for item in only:
        relay(observations, engine, clock, item)
    await clock.advance(0.01)
    for item in only:
        led(observations, engine, clock, item)


async def test_t05_ready_members_receive_one_group_set(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=180), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(2.4)
    assert topics(transport).count(GROUP_TOPIC) == 0
    await clock.advance(0.3)
    assert [x.payload for x in transport.sent if x.topic == GROUP_TOPIC] == [
        {"state": "ON", "brightness": 180, "transition": 0}
    ]
    assert not [x for x in topics(transport) if x.startswith("example/led_")]
    assert all(x.retain is False for x in transport.sent)
    await clock.advance(10)
    assert topics(transport).count(GROUP_TOPIC) == 1


async def test_t06_timeout_degrades_to_ready_members_only(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=90), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "a")
    relay(observations, engine, clock, "b")
    await clock.advance(0.01)
    led(observations, engine, clock, "a")
    await clock.advance(7.9)
    assert not [x for x in topics(transport) if "group" in x or "led_" in x]
    await clock.advance(0.5)
    sent = [(x.topic, x.payload) for x in transport.sent if "relay" not in x.topic]
    assert sent == [
        ("example/led_a/set", {"state": "ON", "brightness": 90, "transition": 0})
    ]
    assert engine.snapshot("b").phase == Phase.WAITING_READY
    assert engine.snapshot("b").reason == "readiness_delayed"
    await clock.advance(10)
    assert "example/led_b/set" not in topics(transport)
    assert GROUP_TOPIC not in topics(transport)


async def test_member_error_degrades_without_waiting_for_barrier(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "a")
    await clock.advance(0.01)
    led(observations, engine, clock, "a")
    await clock.advance(3.0)
    assert engine.snapshot("b").phase == Phase.ERROR
    await clock.advance(0.2)
    assert [x.topic for x in transport.sent if "relay" not in x.topic] == [
        "example/led_a/set"
    ]


@pytest.mark.parametrize("rig", ["wait_all"], indirect=True)
async def test_wait_all_does_not_fall_back_to_individual_on_member_power_error(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "a")
    await clock.advance(0.01)
    led(observations, engine, clock, "a")
    await clock.advance(12)
    assert engine.snapshot("b").phase == Phase.ERROR
    assert not [x for x in topics(transport) if "relay" not in x]


async def test_member_off_cancels_pending_group_on(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=120), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(2.0)
    engine.submit(("b",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "b", SwitchState.OFF)
    await clock.advance(5)
    assert GROUP_TOPIC not in topics(transport)
    assert "example/led_b/set" not in topics(transport)
    assert {"state_b": "OFF"} in [x.payload for x in transport.sent]
    assert [x.payload for x in transport.sent if x.topic == "example/led_a/set"] == [
        {"state": "ON", "brightness": 120, "transition": 0}
    ]
    assert engine.snapshot("b").desired.on is False


async def test_t14_membership_change_blocks_group_send(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(2.5)
    engine.topology_changed(1)
    await clock.advance(10)
    assert GROUP_TOPIC not in topics(transport)
    assert not [x for x in topics(transport) if x.startswith("example/led_")]
    assert not engine.snapshot("a").desired.on
    with pytest.raises(RuntimeError):
        coordinator.submit("group", IntentPatch(on=True), source="ui")


async def test_group_publish_does_not_confirm_all_members(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=180), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(2.7)
    assert topics(transport).count(GROUP_TOPIC) == 1
    snapshot = coordinator.snapshot("group")
    assert snapshot.on is True
    assert (snapshot.confirmed_members, snapshot.member_count) == (0, 2)
    assert snapshot.confirmed is False
    assert engine.snapshot("a").phase == Phase.UNCONFIRMED
    led(observations, engine, clock, "a", brightness=180)
    await clock.advance(0.01)
    snapshot = coordinator.snapshot("group")
    assert (snapshot.confirmed_members, snapshot.confirmed) == (1, False)
    led(observations, engine, clock, "b", brightness=180)
    await clock.advance(0.01)
    snapshot = coordinator.snapshot("group")
    assert (snapshot.confirmed_members, snapshot.confirmed) == (2, True)


async def test_stale_group_after_one_member_change_replans_other_members(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=180), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(2.55)
    engine.submit(("b",), IntentPatch(brightness=50), source="ui")
    await clock.advance(1)
    assert GROUP_TOPIC not in topics(transport)
    assert [x.payload for x in transport.sent if x.topic == "example/led_a/set"] == [
        {"state": "ON", "brightness": 180, "transition": 0}
    ]
    assert [x.payload for x in transport.sent if x.topic == "example/led_b/set"] == [
        {"state": "ON", "brightness": 50, "transition": 0}
    ]


async def test_group_off_sends_only_relay_off_commands(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(3)
    before = len(transport.sent)
    coordinator.submit("group", IntentPatch(on=False), source="ui")
    await clock.advance(0.05)
    assert {tuple(x.payload) for x in transport.sent[before:]} == {
        ("state_a",),
        ("state_b",),
    }
    assert all(list(x.payload.values()) == ["OFF"] for x in transport.sent[before:])
    assert all(x.topic == "example/relay/set" for x in transport.sent[before:])


async def test_non_uniform_member_targets_are_sent_individually(rig):
    clock, transport, observations, engine, coordinator = rig
    engine.submit(("a",), IntentPatch(brightness=100), source="ui")
    engine.submit(("b",), IntentPatch(brightness=200), source="ui")
    coordinator.submit("group", IntentPatch(on=True), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(3)
    assert GROUP_TOPIC not in topics(transport)
    assert [x.payload for x in transport.sent if x.topic == "example/led_a/set"] == [
        {"state": "ON", "brightness": 100, "transition": 0}
    ]
    assert [x.payload for x in transport.sent if x.topic == "example/led_b/set"] == [
        {"state": "ON", "brightness": 200, "transition": 0}
    ]


async def test_group_never_targets_member_that_is_not_part_of_operation(rig):
    clock, transport, observations, engine, coordinator = rig
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await power_both(clock, observations, engine)
    await clock.advance(3)
    led(observations, engine, clock, "a", brightness=100)
    led(observations, engine, clock, "b", brightness=100)
    await clock.advance(0.01)
    engine.submit(("b",), IntentPatch(on=False), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "b", SwitchState.OFF)
    await clock.advance(1)
    before = len(transport.sent)
    coordinator.submit("group", IntentPatch(on=True, brightness=100), source="ui")
    await clock.advance(0.01)
    relay(observations, engine, clock, "b")
    await clock.advance(0.01)
    led(observations, engine, clock, "b")
    await clock.advance(3)
    assert GROUP_TOPIC not in [x.topic for x in transport.sent[before:]]
    assert "example/led_a/set" not in [x.topic for x in transport.sent[before:]]


async def test_unknown_group_is_rejected(rig):
    _, _, _, _, coordinator = rig
    with pytest.raises(KeyError):
        coordinator.submit("other", IntentPatch(on=True), source="ui")
