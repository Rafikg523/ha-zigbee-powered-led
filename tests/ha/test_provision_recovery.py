"""Group maintenance uses MQTT hints without granting light control authority."""

# ruff: noqa: F811

import pytest

from custom_components.zigbee_led_orchestrator.models import (
    BridgeObservation,
    EvidenceQuality,
    IntentPatch,
    RelayObservation,
    SwitchState,
)
from tests.ha.test_runtime import runtime  # noqa: F401


async def test_restart_blocks_new_members_of_an_unfinished_edit():
    from custom_components.zigbee_led_orchestrator.configuration import validate_config
    from custom_components.zigbee_led_orchestrator.runtime import Runtime
    from tests.ha.test_runtime import FakeAdapter
    from tests.unit.test_multi_group_scale import make_scale_config, make_scale_topology

    raw = make_scale_config(num_groups=1, circuits_per_group=3)
    raw["groups"][0]["members"] = ["light_1", "light_2"]
    adapter = FakeAdapter()
    adapter.topology = make_scale_topology(raw)
    instance = Runtime(
        validate_config(raw),
        adapter,
        management_pending="group_1",
        management_members=("light_3",),
    )
    await instance.start()
    try:
        adapter.observation(
            BridgeObservation(True, 1, False, EvidenceQuality.UNKNOWN_SOURCE)
        )
        with pytest.raises(RuntimeError, match="management pending"):
            instance.submit(("light_3",), IntentPatch(on=True), source="ui")
        assert not instance.snapshot("light_3").available
        assert not adapter.sent
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "retained, quality",
    [
        (True, EvidenceQuality.UNKNOWN_SOURCE),
        (False, EvidenceQuality.CACHE),
    ],
)
async def test_unreliable_power_hint_never_provisions_member(
    runtime, retained, quality
):
    instance, adapter = runtime
    instance._options_ready.add("group_a_orchestrator")
    adapter.topology.update_groups(
        [{"id": 1, "friendly_name": "group_a_orchestrator", "members": []}]
    )
    adapter.observation(
        BridgeObservation(True, 1, False, EvidenceQuality.UNKNOWN_SOURCE)
    )
    adapter.observation(
        RelayObservation("relay_a", SwitchState.ON, 2, retained, quality)
    )
    assert instance._plan_action() is None
    assert not adapter.sent


async def test_rejected_group_does_not_block_other_groups_and_retries(monkeypatch):
    from custom_components.zigbee_led_orchestrator.configuration import validate_config
    from custom_components.zigbee_led_orchestrator.runtime import Runtime
    from tests.ha.test_runtime import FakeAdapter
    from tests.helpers.clock import FakeClock
    from tests.unit.test_multi_group_scale import make_scale_config, make_scale_topology

    clock = FakeClock(start=1)
    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.runtime.MonotonicClock",
        lambda: clock,
    )
    raw = make_scale_config(num_groups=2, circuits_per_group=2)
    adapter = FakeAdapter()
    adapter.topology = make_scale_topology(raw)
    calls = []
    refused = True

    async def request(path, payload):
        calls.append((path, payload))
        if payload["id"] == "Room 1_orchestrator" and refused:
            return {"status": "error"}
        return {"status": "ok", "data": {"to": {"optimistic": False, "retain": False}}}

    adapter.request_bridge = request
    instance = Runtime(validate_config(raw), adapter)
    await instance.start()
    try:
        adapter.observation(
            BridgeObservation(True, clock.now(), False, EvidenceQuality.UNKNOWN_SOURCE)
        )
        await clock.advance(0)
        assert any(payload["id"] == "Room 2_orchestrator" for _, payload in calls)
        assert len(calls) == 2
        refused = False
        await clock.advance(10)
        assert "Room 1_orchestrator" in instance._options_ready
        assert len(calls) == 3
        assert not adapter.sent
    finally:
        await instance.close()


async def test_five_groups_sync_fifty_members_and_restart_without_light_commands(
    monkeypatch,
):
    from custom_components.zigbee_led_orchestrator.configuration import validate_config
    from custom_components.zigbee_led_orchestrator.runtime import Runtime
    from tests.ha.test_runtime import FakeAdapter
    from tests.helpers.clock import FakeClock
    from tests.unit.test_multi_group_scale import make_scale_config, make_scale_topology

    clock = FakeClock(start=1)
    monkeypatch.setattr(
        "custom_components.zigbee_led_orchestrator.runtime.MonotonicClock",
        lambda: clock,
    )
    raw = make_scale_config()
    topology = make_scale_topology(raw)
    catalog = [{"id": 200, "friendly_name": "manual", "members": []}]
    topology.update_groups(catalog)
    calls = []
    devices = {device.name: device.ieee for device in topology.devices()}

    for restart in range(2):
        adapter = FakeAdapter()
        adapter.topology = topology

        async def request(path, payload, *, adapter=adapter):
            calls.append((path, payload))
            if path == "group/add":
                catalog.append({**payload, "members": []})
                response = {"status": "ok", "data": payload}
            elif path == "group/options":
                response = {"status": "ok", "data": {"to": payload["options"]}}
            elif path == "group/members/add":
                group = next(
                    g for g in catalog if g["friendly_name"] == payload["group"]
                )
                group["members"].append(
                    {
                        "ieee_address": devices[payload["device"]],
                        "endpoint": payload["endpoint"],
                    }
                )
                response = {"status": "ok", "data": payload}
            else:
                pytest.fail(f"Unexpected maintenance request: {path}")
            topology.update_groups(catalog)
            adapter.topology_callback(topology.version)
            return response

        adapter.request_bridge = request
        instance = Runtime(validate_config(raw), adapter)
        await instance.start()
        try:
            adapter.observation(
                BridgeObservation(
                    True, clock.now(), False, EvidenceQuality.UNKNOWN_SOURCE
                )
            )
            for channel in raw["relay_channels"]:
                adapter.observation(
                    RelayObservation(
                        channel["id"],
                        SwitchState.ON,
                        clock.now(),
                        False,
                        EvidenceQuality.UNKNOWN_SOURCE,
                    )
                )
            await clock.advance(0)
            assert len(catalog) == 6
            assert catalog[0] == {"id": 200, "friendly_name": "manual", "members": []}
            assert [len(g["members"]) for g in catalog[1:]] == [10, 10, 10, 10, 10]
            assert not adapter.sent
            assert all(
                not instance.snapshot(light["id"]).desired.on for light in raw["lights"]
            )
            assert sum(path == "group/add" for path, _ in calls) == 5
            assert sum(path == "group/members/add" for path, _ in calls) == 50
            assert sum(path == "group/options" for path, _ in calls) == 5 * (
                restart + 1
            )
        finally:
            await instance.close()
        await clock.advance(1)
