"""Real adapter and raw ZCL responses: no seeded relay OFF or trust override."""

import pytest

from tests.scenarios.harness import GROUP, LED, RELAY
from tests.unit.test_configuration import valid_config

pytestmark = pytest.mark.parametrize("rig", [True], indirect=True)


@pytest.fixture
async def pilot(rig):
    config = valid_config()
    config["report_extension"] = True
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
    await rig.enable_active()
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
