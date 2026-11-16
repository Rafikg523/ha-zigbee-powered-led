"""Two-light MVP scenarios through a real config entry and fake HA MQTT.

Reports are injected as MQTT messages and commands are observed at the HA MQTT
publish boundary. Time is simulated. Reports are classified by an adapter model
that trusts non-retained messages (see harness); this is a synthetic assumption,
not proof of how the real installation behaves.
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zigbee_led_orchestrator.models import ControlMode
from tests.scenarios.harness import GROUP, LED, RELAY

RELAY_SET = f"{RELAY}/set"
LED_SET = {light: f"{topic}/set" for light, topic in LED.items()}
GROUP_SET = f"{GROUP}/set"
# Fixture ranges: Z2M brightness 1..254 (HA 180 -> 179) and mired 153..500 (3000 K -> 333).
LED_A_REPORT = {"brightness": 179, "mired": 333}


@pytest.fixture
async def ready(rig):
    await rig.setup()
    await rig.boot_to_off()
    return rig


async def light_a_on(rig):
    """Drive light_a from OFF to a confirmed ON through the normal service path."""
    await rig.light("turn_on", "light_a", brightness=180, color_temp_kelvin=3000)
    await rig.advance(0.01)
    await rig.report_relay("ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.advance(3.0)
    assert rig.publications()[-1] == (
        LED_SET["light_a"],
        {"state": "ON", "transition": 0, "brightness": 179, "color_temp": 333},
    )
    await rig.report_led("light_a", "ON", **LED_A_REPORT)


async def command_is_inert(rig, service, suffix):
    """HA ignores service calls to unavailable entities; either way nothing changes."""
    desired = {light: rig.runtime.snapshot(light).desired for light in LED}
    published = rig.publications()
    try:
        await rig.light(service, suffix)
    except HomeAssistantError:
        pass
    await rig.advance(1)
    assert {light: rig.runtime.snapshot(light).desired for light in LED} == desired
    assert rig.publications() == published


def topics(rig):
    return [topic for topic, _ in rig.publications()]


async def test_t01_off_to_on_waits_for_relay_and_readiness(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=180, color_temp_kelvin=3000)
    await rig.advance(0.01)
    assert rig.publications() == [(RELAY_SET, {"state_l1": "ON"})]
    assert rig.state("light_a").state == "on"
    assert rig.state("light_a_confirmed").state == "off"

    await rig.report_relay("ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.advance(2.0)
    assert len(rig.publications()) == 1

    await rig.advance(1.0)
    assert rig.publications() == [
        (RELAY_SET, {"state_l1": "ON"}),
        (
            LED_SET["light_a"],
            {"state": "ON", "transition": 0, "brightness": 179, "color_temp": 333},
        ),
    ]
    assert rig.state("light_a_phase").state == "UNCONFIRMED"
    assert rig.state("light_a_confirmed").state == "off"

    await rig.report_led("light_a", "ON", **LED_A_REPORT)
    assert rig.state("light_a_phase").state == "ON"
    assert rig.state("light_a_confirmed").state == "on"
    assert rig.state("light_a_effective_state").state == "ON_REPORTED"
    assert rig.runtime.snapshot("light_b").desired.on is False
    await rig.advance(30)
    assert len(rig.publications()) == 2


async def test_t02_off_publishes_only_relay_off(ready):
    rig = ready
    await light_a_on(rig)
    before = len(rig.publications())
    await rig.light("turn_off", "light_a")
    await rig.advance(0.01)
    assert rig.publications()[before:] == [(RELAY_SET, {"state_l1": "OFF"})]
    assert rig.state("light_a").state == "off"
    await rig.report_relay("OFF")
    await rig.advance(10)
    assert rig.state("light_a_phase").state == "OFF"
    assert rig.state("light_a_confirmed").state == "on"
    assert LED_SET["light_a"] not in topics(rig)[before:]
    assert len(rig.publications()) == before + 1


async def test_t03_rapid_toggles_end_in_last_intent_without_replay(ready):
    rig = ready
    for index in range(10):
        await rig.light("turn_on" if index % 2 == 0 else "turn_off", "light_a")
        await rig.advance(0.005)
    await rig.advance(10)
    assert rig.runtime.snapshot("light_a").desired.on is False
    published = rig.publications()
    assert all(topic == RELAY_SET for topic, _ in published)
    states = [payload["state_l1"] for _, payload in published]
    assert states and states[-1] == "OFF"
    assert states.count("ON") <= 1
    count = len(published)
    await rig.report_relay("OFF")
    await rig.advance(30)
    assert len(rig.publications()) == count


async def test_t04_twenty_slider_updates_are_coalesced(ready):
    rig = ready
    await light_a_on(rig)
    before = len(rig.publications())
    for value in range(1, 21):
        await rig.light("turn_on", "light_a", brightness=value * 10)
        await rig.advance(0.01)
    await rig.advance(5)
    sets = [
        payload
        for topic, payload in rig.publications()[before:]
        if topic == LED_SET["light_a"]
    ]
    assert 1 <= len(sets) <= 3
    assert sets[-1]["brightness"] == round(200 * 254 / 255)
    assert rig.runtime.snapshot("light_a").desired.brightness == 200
    assert all(topic == LED_SET["light_a"] for topic in topics(rig)[before:])


async def test_t05_group_sends_one_native_set_after_barrier(ready):
    rig = ready
    await rig.light("turn_on", "group_a", brightness=200)
    await rig.advance(0.5)
    relay_payloads = [p for t, p in rig.publications() if t == RELAY_SET]
    assert {key for payload in relay_payloads for key in payload} == {
        "state_l1",
    }
    await rig.report_relay("ON", "OFF")
    await rig.advance(0.15)
    relay_payloads = [p for t, p in rig.publications() if t == RELAY_SET]
    assert {key for payload in relay_payloads for key in payload} == {
        "state_l1",
        "state_l2",
    }
    assert all(value == "ON" for p in relay_payloads for value in p.values())
    assert set(topics(rig)) == {RELAY_SET}

    await rig.report_relay("ON", "ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.report_led("light_b", "ON")
    await rig.advance(3.5)
    assert [t for t in topics(rig) if t != RELAY_SET] == [GROUP_SET]
    payload = dict(rig.publications()[-1][1])
    assert payload["state"] == "ON"
    assert payload["brightness"] == round(200 * 254 / 255)

    await rig.report_led("light_a", "ON", brightness=payload["brightness"])
    await rig.report_led("light_b", "ON", brightness=payload["brightness"])
    for light in ("light_a", "light_b"):
        assert rig.runtime.snapshot(light).desired.on
    await rig.advance(30)
    assert topics(rig).count(GROUP_SET) == 1
    assert LED_SET["light_a"] not in topics(rig)
    assert LED_SET["light_b"] not in topics(rig)


async def test_t06_unresponsive_led_degrades_only_its_circuit(ready):
    rig = ready
    await rig.light("turn_on", "light_b")
    await rig.advance(0.01)
    await rig.report_relay("OFF", "ON")
    await rig.advance(9)
    snapshot = rig.runtime.snapshot("light_b")
    assert snapshot.phase.value == "WAITING_READY"
    assert snapshot.reason == "readiness_delayed"
    assert not snapshot.confirmed
    assert rig.publications() == [(RELAY_SET, {"state_l2": "ON"})]
    assert rig.diagnostics()["counters"]["timeouts"] >= 1

    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    await rig.report_relay("ON", "ON")
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.advance(3.0)
    assert LED_SET["light_a"] in topics(rig)
    assert LED_SET["light_b"] not in topics(rig)
    count = len(rig.publications())
    await rig.advance(60)
    assert len(rig.publications()) == count


async def test_t07_physical_off_is_not_undone(ready):
    rig = ready
    await light_a_on(rig)
    before = len(rig.publications())
    await rig.report_relay("OFF", "OFF")
    await rig.advance(60)
    snapshot = rig.runtime.snapshot("light_a")
    assert snapshot.desired.on is False
    assert snapshot.desired.source == "external_relay"
    assert rig.state("light_a").state == "off"
    assert len(rig.publications()) == before
    assert rig.diagnostics()["counters"]["conflicts"] == 1


async def test_t08_physical_on_starts_new_epoch_without_publishing(ready):
    rig = ready
    await rig.report_relay("OFF", "OFF")
    await rig.advance(1)
    epoch = rig.diagnostics()["lights"]["light_1"]["observed"]["power_epoch"]
    await rig.report_relay("ON", "OFF")
    after = rig.diagnostics()["lights"]["light_1"]["observed"]["power_epoch"]
    assert after == epoch + 1
    assert rig.runtime.snapshot("light_a").desired.on is True
    assert rig.runtime.snapshot("light_a").desired.source == "external_relay"
    await rig.advance(3.0)
    await rig.report_led("light_a", "ON", **LED_A_REPORT)
    await rig.advance(5)
    assert rig.state("light_a_phase").state == "ON"
    assert rig.publications() == []


async def test_t10_restart_with_relay_off_never_turns_on(ready, hass_storage):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=120, color_temp_kelvin=3000)
    await rig.advance(0.01)
    await rig.light("turn_off", "light_a")
    await rig.advance(0.01)
    await rig.report_relay("OFF", "OFF")
    await rig.advance(1)
    rig.mqtt_mock.async_publish.reset_mock()

    await rig.reload()
    stored = hass_storage[f"zigbee_led_orchestrator.{rig.entry.entry_id}"]["data"]
    assert stored == {
        "version": 1,
        "lights": {"light_a": {"brightness": 120, "color_temp_kelvin": 3000}},
    }
    await rig.publish_catalog()
    await rig.report_relay("OFF", "OFF", retain=True)
    await rig.advance(1)
    assert rig.runtime.mode == ControlMode.ACTIVE
    light = rig.runtime.snapshot("light_a")
    assert light.desired.on is False
    assert (light.desired.brightness, light.desired.color_temp_kelvin) == (120, 3000)
    await rig.advance(30)
    assert rig.publications() == []
    assert rig.runtime.snapshot("light_a").desired.on is False


async def test_t11_bridge_restart_does_not_replay_history(ready):
    rig = ready
    await light_a_on(rig)
    before = len(rig.publications())
    rig.fire("zigbee2mqtt/bridge/state", {"state": "offline"})
    await rig.settle()
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.state("light_a").state == "unavailable"
    await command_is_inert(rig, "turn_on", "light_b")

    await rig.publish_catalog()
    await rig.report_relay("ON", "OFF", retain=True)
    await rig.report_led("light_a", "ON", retain=True, **LED_A_REPORT)
    await rig.advance(60)
    assert len(rig.publications()) == before
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.state("light_a_confirmed").state == "off"
    assert rig.runtime.snapshot("light_a").desired.on is False
    assert not rig.runtime.snapshot("light_a").confirmed


async def test_t12_retained_reports_never_prove_readiness(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    await rig.report_relay("ON", "OFF", retain=True)
    await rig.report_led("light_a", "ON", retain=True)
    await rig.advance(10)
    assert rig.publications() == [(RELAY_SET, {"state_l1": "ON"})]
    snapshot = rig.runtime.snapshot("light_a")
    assert snapshot.phase.value == "ERROR"
    assert snapshot.effective.value != "ON_REPORTED"
    assert not snapshot.confirmed
    assert rig.state("light_a_confirmed").state == "off"


async def test_t12_led_report_older_than_new_power_epoch_is_not_readiness(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    await rig.report_led("light_a", "ON")
    await rig.advance(0.5)
    await rig.report_relay("ON", "OFF")
    await rig.advance(5)
    assert LED_SET["light_a"] not in topics(rig)
    await rig.report_led("light_a", "ON")
    await rig.advance(0.5)
    assert topics(rig).count(LED_SET["light_a"]) == 1


async def test_t14_group_membership_change_stops_risky_operations(ready):
    rig = ready
    before = len(rig.publications())
    changed = [dict(rig.pilot["groups"][0])]
    changed[0]["members"] = changed[0]["members"][:1]
    rig.fire("zigbee2mqtt/bridge/groups", changed)
    await rig.settle()
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.state("group_a").state == "unavailable"
    await command_is_inert(rig, "turn_on", "group_a")
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    assert len(rig.publications()) == before + 1
    assert GROUP_SET not in topics(rig)
    assert any(key.startswith("group_") for key in rig.runtime.notices)


async def test_t15_mqtt_reconnect_does_not_replay_pending_on(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(0.01)
    assert len(rig.publications()) == 1
    await rig.set_connection(False)
    assert rig.state("light_a").state == "unavailable"
    await rig.advance(20)
    await rig.set_connection(True)
    await rig.publish_catalog()
    await rig.report_relay("ON", "OFF", retain=True)
    await rig.advance(60)
    assert len(rig.publications()) == 1
    assert rig.runtime.mode == ControlMode.ACTIVE
    assert rig.runtime.snapshot("light_a").desired.on is False


async def test_t16_slow_led_boot_does_not_repeat_commands(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=160)
    await rig.advance(0.01)
    await rig.report_relay("ON", "OFF")
    await rig.advance(3.0)
    assert len(rig.publications()) == 1
    await rig.report_led("light_a", "ON")
    await rig.advance(0.5)
    assert [t for t in topics(rig)] == [RELAY_SET, LED_SET["light_a"]]
    await rig.advance(60)
    assert len(rig.publications()) == 2


async def test_t16_relay_report_after_deadline_does_not_start_led_set(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=160)
    await rig.advance(10)
    assert rig.runtime.snapshot("light_a").reason == "relay_confirmation_timeout"
    await rig.report_relay("ON", "OFF")
    await rig.report_led("light_a", "ON")
    await rig.advance(30)
    assert rig.publications() == [(RELAY_SET, {"state_l1": "ON"})]


async def test_unload_during_powering_stops_all_commands(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=160)
    await rig.advance(0.01)
    await rig.report_relay("ON", "OFF")
    await rig.advance(0.5)
    assert await rig.hass.config_entries.async_unload(rig.entry.entry_id)
    assert rig.adapters[0]._closed
    count = len(rig.publications())
    await rig.report_led("light_a", "ON")
    await rig.advance(60)
    assert len(rig.publications()) == count == 1


async def test_bridge_loss_during_powering_stops_led_set(ready):
    rig = ready
    await rig.light("turn_on", "light_a", brightness=160)
    await rig.advance(0.01)
    await rig.report_relay("ON", "OFF")
    await rig.advance(0.5)
    rig.fire("zigbee2mqtt/bridge/state", {"state": "offline"})
    await rig.settle()
    await rig.advance(60)
    assert rig.publications() == [(RELAY_SET, {"state_l1": "ON"})]
    assert rig.runtime.mode == ControlMode.OBSERVE
    assert rig.runtime.snapshot("light_a").phase.value == "ERROR"


async def test_unproven_report_source_blocks_all_commands(rig):
    await rig.setup()
    rig.adapters[0].trust_reports = False
    await rig.boot_to_off()
    await rig.light("turn_on", "light_a", brightness=180)
    await rig.advance(30)
    assert rig.publications() == []
    snapshot = rig.runtime.snapshot("light_a")
    assert snapshot.phase.value == "ERROR"
    assert snapshot.reason == "relay_state_unknown"
    assert not snapshot.confirmed
