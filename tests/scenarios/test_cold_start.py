"""Real adapter and raw ZCL responses: no seeded relay OFF or trust override."""

import pytest

from tests.scenarios.harness import GROUP, LED, RELAY
from tests.unit.test_configuration import valid_config

pytestmark = pytest.mark.parametrize("rig", [True], indirect=True)


@pytest.fixture
async def pilot(rig, request):
    config = valid_config()
    config["report_extension"] = True
    config["policies"]["group_readiness_strategy"] = getattr(
        request, "param", "individual"
    )
    await rig.setup(config)
    await rig.publish_catalog()
    request = next(
        p
        for t, p in reversed(rig.publications())
        if t.endswith("/orchestrator/subscribe")
    )
    session = request["session"]
    rig.fire(
        "zigbee2mqtt/orchestrator/report",
        {"session": session, "sequence": 0, "type": "ready"},
    )
    await rig.settle()
    sequence = 0

    async def raw(ieee, cluster, data, *, retain=False, endpoint=1):
        nonlocal sequence
        next_sequence = sequence + 1
        rig.fire(
            "zigbee2mqtt/orchestrator/report",
            {
                "session": session,
                "sequence": next_sequence,
                "type": "readResponse",
                "ieee": ieee,
                "endpoint": endpoint,
                "cluster": cluster,
                "data": data,
            },
            retain=retain,
        )
        if not retain:
            sequence = next_sequence
        await rig.settle()
        await rig.advance(0.001)

    return rig, raw


def traffic(rig):
    return [(t, p) for t, p in rig.publications() if t.endswith(("/set", "/get"))]


@pytest.mark.parametrize("pilot", ["individual", "wait_all"], indirect=True)
async def test_late_group_member_recovers_through_real_report_adapter(pilot, request):
    rig, raw = pilot
    relay = "0x0000000000000001"
    await rig.light("turn_on", "group_a", brightness=200)
    await rig.advance(0.25)
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 0}, endpoint=endpoint)
    await rig.advance(0.65)
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 1}, endpoint=endpoint)
    await rig.advance(3)
    await raw("0x0000000000000002", "genOnOff", {"onOff": 1})
    await rig.advance(10)
    assert rig.phase("light_b") == "WAITING_READY"
    assert rig.runtime.snapshot("light_b").reason == "readiness_delayed"
    before = traffic(rig)
    await rig.advance(30)
    assert traffic(rig) == before
    await raw("0x0000000000000003", "genOnOff", {"onOff": 1})
    await rig.advance(0.5)
    sets = [
        (t, p) for t, p in traffic(rig) if t.endswith("/set") and t != RELAY + "/set"
    ]
    expected_topics = (
        [GROUP + "/set"]
        if request.node.callspec.params["pilot"] == "wait_all"
        else [LED["light_a"] + "/set", LED["light_b"] + "/set"]
    )
    assert [t for t, _ in sets] == expected_topics
    assert all(
        p == {"state": "ON", "brightness": 199, "transition": 0} for _, p in sets
    )
    for ieee in ("0x0000000000000002", "0x0000000000000003"):
        await raw(ieee, "genOnOff", {"onOff": 1})
        await raw(ieee, "genLevelCtrl", {"currentLevel": 199})
    assert rig.runtime.group_snapshot("group_a").confirmed


async def test_restart_with_relay_off_and_no_spontaneous_reports_runs_two_cycles(pilot):
    rig, raw = pilot
    relay, led = "0x0000000000000001", "0x0000000000000002"
    assert traffic(rig) == []
    await rig.light("turn_on", "light_a", brightness=180, color_temp_kelvin=3000)
    await rig.advance(0.01)
    assert traffic(rig) == [(RELAY + "/get", {"state_l1": ""})]
    # Ordinary optimistic/cache state and retained raw replies cannot unlock ON.
    await rig.report_relay("OFF")
    await raw(relay, "genOnOff", {"onOff": 0}, retain=True)
    assert len(traffic(rig)) == 1
    await raw(relay, "genOnOff", {"onOff": 0})
    for cycle in range(2):
        if cycle:
            await rig.light("turn_on", "light_a")
        await rig.advance(0.41)
        assert traffic(rig)[-1] == (RELAY + "/set", {"state_l1": "ON"})
        await raw(relay, "genOnOff", {"onOff": 1})
        await rig.advance(2.51)
        assert traffic(rig)[-1] == (LED["light_a"] + "/get", {"state": ""})
        await raw(led, "genOnOff", {"onOff": 1})
        await rig.advance(0.25)
        assert traffic(rig)[-2:] == [
            (
                LED["light_a"] + "/set",
                {
                    "state": "ON",
                    "brightness": 179,
                    "color_temp": 333,
                    "transition": 0,
                },
            ),
            (
                LED["light_a"] + "/get",
                {"state": "", "brightness": "", "color_temp": ""},
            ),
        ]
        if cycle == 0:
            await rig.advance(3)  # Responses arriving after the verification window.
        # Unchanged attributes are read, rather than assuming a change report.
        await raw(led, "genOnOff", {"onOff": 1})
        await raw(led, "genLevelCtrl", {"currentLevel": 179})
        assert not rig.runtime.snapshot("light_a").confirmed
        await raw(led, "lightingColorCtrl", {"colorTemperature": 333})
        assert rig.runtime.snapshot("light_a").confirmed
        await rig.light("turn_off", "light_a")
        await rig.advance(0.25)
        assert traffic(rig)[-2:] == [
            (LED["light_a"] + "/set", {"state": "OFF", "transition": 0}),
            (LED["light_a"] + "/get", {"state": ""}),
        ]
        assert rig.runtime.snapshot("light_a").desired.brightness == 180
        await raw(led, "genOnOff", {"onOff": 0}, retain=True)
        assert traffic(rig)[-1][0] == LED["light_a"] + "/get"
        await raw(led, "genOnOff", {"onOff": 0})
        await rig.advance(0.15)
        assert traffic(rig)[-1] == (RELAY + "/set", {"state_l1": "OFF"})
        await raw(relay, "genOnOff", {"onOff": 0})
        assert rig.phase("light_a") == "OFF"
    assert (
        sum(t == RELAY + "/set" and p == {"state_l1": "ON"} for t, p in traffic(rig))
        == 2
    )


async def test_off_while_waiting_for_initial_read_does_not_replay_on(pilot):
    rig, raw = pilot
    await rig.light("turn_on", "light_a")
    await rig.advance(0.01)
    await rig.light("turn_off", "light_a")
    await rig.advance(0.15)
    await raw("0x0000000000000001", "genOnOff", {"onOff": 0})
    await rig.advance(12)
    assert not any(p.get("state_l1") == "ON" for _, p in traffic(rig))
    assert rig.phase("light_a") == "OFF"


async def test_existing_group_uses_one_set_and_individual_read_confirmations(pilot):
    rig, raw = pilot
    relay = "0x0000000000000001"
    await rig.light("turn_on", "group_a", brightness=200)
    await rig.advance(0.25)
    assert {tuple(p) for t, p in traffic(rig) if t == RELAY + "/get"} == {
        ("state_l1",),
        ("state_l2",),
    }
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 0}, endpoint=endpoint)
    await rig.advance(0.65)
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 1}, endpoint=endpoint)
    await rig.advance(2.75)
    for ieee in ("0x0000000000000002", "0x0000000000000003"):
        await raw(ieee, "genOnOff", {"onOff": 1})
    await rig.advance(0.5)
    sets = [
        (t, p) for t, p in traffic(rig) if t.endswith("/set") and t != RELAY + "/set"
    ]
    assert sets == [
        (GROUP + "/set", {"state": "ON", "brightness": 199, "transition": 0})
    ]
    for ieee in ("0x0000000000000002", "0x0000000000000003"):
        await raw(ieee, "genOnOff", {"onOff": 1})
        await raw(ieee, "genLevelCtrl", {"currentLevel": 199})
    assert rig.runtime.group_snapshot("group_a").confirmed
    before = len(traffic(rig))
    await rig.light("turn_off", "group_a")
    await rig.advance(0.5)
    shutdown = traffic(rig)[before:]
    assert not any(t == RELAY + "/set" for t, _ in shutdown)
    assert {t for t, p in shutdown if p.get("state") == "OFF"} == {
        LED["light_a"] + "/set",
        LED["light_b"] + "/set",
    }
    for ieee in ("0x0000000000000002", "0x0000000000000003"):
        await raw(ieee, "genOnOff", {"onOff": 0})
    await rig.advance(0.25)
    assert sum(t == RELAY + "/set" for t, _ in traffic(rig)[before:]) == 2
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 0}, endpoint=endpoint)
    assert rig.phase("light_a") == rig.phase("light_b") == "OFF"


async def test_off_while_led_is_booting_cancels_settings(pilot):
    rig, raw = pilot
    relay = "0x0000000000000001"
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    await raw(relay, "genOnOff", {"onOff": 0})
    await rig.advance(0.41)
    await raw(relay, "genOnOff", {"onOff": 1})
    await rig.advance(2.51)
    assert traffic(rig)[-1][0] == LED["light_a"] + "/get"
    await rig.light("turn_off", "light_a")
    await rig.advance(0.15)
    await raw(relay, "genOnOff", {"onOff": 0})
    await raw("0x0000000000000002", "genOnOff", {"onOff": 1})
    await rig.advance(10)
    assert not any(
        t == LED["light_a"] + "/set" and p.get("state") != "OFF"
        for t, p in traffic(rig)
    )
    assert rig.phase("light_a") == "OFF"


@pytest.mark.parametrize("during_boot", [False, True])
async def test_disconnect_during_read_never_replays_old_on(pilot, during_boot):
    rig, raw = pilot
    await rig.light("turn_on", "light_a")
    await rig.advance(0.01)
    if during_boot:
        await raw("0x0000000000000001", "genOnOff", {"onOff": 0})
        await rig.advance(0.41)
        await raw("0x0000000000000001", "genOnOff", {"onOff": 1})
        await rig.advance(2.51)
    before = traffic(rig)
    await rig.set_connection(False)
    await rig.set_connection(True)
    await raw("0x0000000000000001", "genOnOff", {"onOff": 1})
    await rig.advance(12)
    assert traffic(rig) == before
    assert not rig.runtime.snapshot("light_a").desired.on


@pytest.mark.parametrize("pilot", ["individual", "wait_all"], indirect=True)
async def test_nested_ha_groups_coalesce_slider_and_share_repeated_off(pilot):
    from pytest_homeassistant.common import MockConfigEntry

    rig, raw = pilot
    relay = "0x0000000000000001"
    leds = ("0x0000000000000002", "0x0000000000000003")
    await rig.light("turn_on", "group_a", brightness=100)
    await rig.advance(0.25)
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 0}, endpoint=endpoint)
    await rig.advance(0.65)
    for endpoint in (1, 2):
        await raw(relay, "genOnOff", {"onOff": 1}, endpoint=endpoint)
    await rig.advance(3)
    for ieee in leds:
        await raw(ieee, "genOnOff", {"onOff": 1})
    await rig.advance(0.5)
    for ieee in leds:
        await raw(ieee, "genOnOff", {"onOff": 1})
        await raw(ieee, "genLevelCtrl", {"currentLevel": 100})
    assert rig.runtime.group_snapshot("group_a").confirmed
    member = rig.entity_id("group_a")
    parents = []
    for name in ("nested_inner", "nested_outer"):
        entry = MockConfigEntry(
            domain="group",
            title=name,
            options={
                "group_type": "light",
                "entities": [member],
                "hide_members": False,
            },
        )
        entry.add_to_hass(rig.hass)
        assert await rig.hass.config_entries.async_setup(entry.entry_id)
        await rig.settle()
        parents.append(entry)
        member = "light." + name
    try:
        before = len(traffic(rig))
        for value in (255, 56, 191, 56):
            await rig.hass.services.async_call(
                "light",
                "turn_on",
                {"entity_id": member, "brightness": value},
                blocking=True,
            )
            await rig.advance(0.3)
        assert [p for t, p in traffic(rig)[before:] if t == GROUP + "/set"] == [
            {"brightness": 254, "transition": 0}
        ]
        for ieee in leds:
            await raw(ieee, "genLevelCtrl", {"currentLevel": 254})
        await rig.advance(0.5)
        assert [p for t, p in traffic(rig)[before:] if t == GROUP + "/set"] == [
            {"brightness": 254, "transition": 0},
            {"brightness": 56, "transition": 0},
        ]
        before_off = len(traffic(rig))
        for _ in range(4):
            await rig.hass.services.async_call(
                "light", "turn_off", {"entity_id": member}, blocking=True
            )
            await rig.advance(0.3)
        await rig.advance(10)
        cuts = [p for t, p in traffic(rig)[before_off:] if t == RELAY + "/set"]
        assert sorted(tuple(p.items()) for p in cuts) == [
            (("state_l1", "OFF"),),
            (("state_l2", "OFF"),),
        ]
    finally:
        for entry in reversed(parents):
            await rig.hass.config_entries.async_unload(entry.entry_id)
