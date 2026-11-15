"""Scale and independence tests for N groups and M circuits."""

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.desired_store import DesiredStateStore
from custom_components.zigbee_led_orchestrator.group_coordinator import GroupCoordinator
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    PolicyConfig,
    RelayObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.observation_store import (
    BridgeObservation,
    ObservationStore,
)
from custom_components.zigbee_led_orchestrator.reconciler import (
    Circuit,
    GroupCircuit,
    Reconciler,
)
from custom_components.zigbee_led_orchestrator.topology import (
    Topology,
    validate_topology,
)
from tests.helpers.clock import FakeClock
from tests.helpers.transport import FakeTransport


def make_scale_config(num_groups=5, circuits_per_group=10):
    relay_channels = []
    lights = []
    groups = []
    circuit_index = 0
    for g in range(num_groups):
        group_id = f"group_{g + 1}"
        group_members = []
        for c in range(circuits_per_group):
            circuit_index += 1
            relay_id = f"relay_{circuit_index}"
            light_id = f"light_{circuit_index}"
            relay_ieee = f"0x{circuit_index:016x}"
            light_ieee = f"0x{(1000 + circuit_index):016x}"
            state_key = "state" if circuit_index % 2 == 1 else "state_l1"

            relay_channels.append(
                {
                    "id": relay_id,
                    "device_ieee": relay_ieee,
                    "endpoint": 1,
                    "state_key": state_key,
                }
            )
            lights.append(
                {
                    "id": light_id,
                    "name": f"Light {circuit_index}",
                    "device_ieee": light_ieee,
                    "endpoint": 1,
                    "power_channel": relay_id,
                    "min_kelvin": 2000,
                    "max_kelvin": 6500,
                }
            )
            group_members.append(light_id)
        groups.append(
            {
                "id": group_id,
                "name": f"Room {g + 1}",
                "z2m_id": g + 1,
                "members": group_members,
            }
        )
    return {
        "base_topic": "zigbee2mqtt",
        "relay_channels": relay_channels,
        "lights": lights,
        "groups": groups,
        "rooms": [],
        "policies": {
            "min_send_interval_ms": 100,
            "publication_timeout_ms": 3000,
            "boot_grace_ms": 1000,
            "readiness_timeout_ms": 5000,
        },
        "report_extension": False,
    }


def make_scale_topology(raw_config):
    topo = Topology(raw_config["base_topic"])
    devices_by_ieee = {}
    for rc in raw_config["relay_channels"]:
        ieee = rc["device_ieee"]
        if ieee not in devices_by_ieee:
            devices_by_ieee[ieee] = {
                "ieee_address": ieee,
                "friendly_name": f"relay_dev_{ieee.replace('0x', '')}",
                "endpoints": {},
                "definition": {"exposes": []},
            }
        ep = str(rc["endpoint"])
        devices_by_ieee[ieee]["endpoints"][ep] = {}
        devices_by_ieee[ieee]["definition"]["exposes"].append(
            {
                "type": "switch",
                "features": [
                    {
                        "name": "state",
                        "property": rc["state_key"],
                        "access": 7,
                        "value_on": "ON",
                        "value_off": "OFF",
                    }
                ],
            }
        )
    for lc in raw_config["lights"]:
        ieee = lc["device_ieee"]
        if ieee not in devices_by_ieee:
            devices_by_ieee[ieee] = {
                "ieee_address": ieee,
                "friendly_name": f"light_dev_{ieee.replace('0x', '')}",
                "endpoints": {},
                "definition": {"exposes": []},
            }
        ep = str(lc["endpoint"])
        devices_by_ieee[ieee]["endpoints"][ep] = {}
        devices_by_ieee[ieee]["definition"]["exposes"].append(
            {
                "type": "light",
                "features": [
                    {
                        "name": "state",
                        "property": "state",
                        "access": 7,
                        "value_on": "ON",
                        "value_off": "OFF",
                    },
                    {
                        "name": "brightness",
                        "property": "brightness",
                        "access": 7,
                        "value_min": 1,
                        "value_max": 254,
                    },
                    {
                        "name": "color_temp",
                        "property": "color_temp",
                        "access": 7,
                        "value_min": 153,
                        "value_max": 500,
                    },
                ],
            }
        )
    topo.update_devices(list(devices_by_ieee.values()))
    lights_by_id = {lc["id"]: lc for lc in raw_config["lights"]}
    groups = []
    for g in raw_config["groups"]:
        members = [
            {
                "ieee_address": lights_by_id[m]["device_ieee"],
                "endpoint": lights_by_id[m]["endpoint"],
            }
            for m in g["members"]
        ]
        groups.append(
            {
                "id": g["z2m_id"],
                "friendly_name": f"{g['name']}_orchestrator",
                "members": members,
            }
        )
    topo.update_groups(groups)
    return topo


def test_5_groups_10_circuits_config_validation():
    raw = make_scale_config(num_groups=5, circuits_per_group=10)
    config = validate_config(raw)
    assert len(config.groups) == 5
    assert len(config.lights) == 50
    assert len(config.relay_channels) == 50
    for g in config.groups:
        assert len(g.members) == 10


def test_5_groups_10_circuits_topology_validation():
    raw = make_scale_config(num_groups=5, circuits_per_group=10)
    config = validate_config(raw)
    topo = make_scale_topology(raw)
    validate_topology(config, topo, require_groups=True)


@pytest.mark.asyncio
async def test_independent_group_control_at_scale():
    raw = make_scale_config(num_groups=5, circuits_per_group=10)
    config = validate_config(raw)
    clock = FakeClock(start=1)
    transport = FakeTransport(clock)
    all_light_ids = tuple(light.id for light in config.lights)
    desired = DesiredStateStore(all_light_ids, clock=clock)
    observations = ObservationStore(
        {light.id: light.power_channel for light in config.lights}
    )
    observations.ingest(
        BridgeObservation(True, clock.now(), False, EvidenceQuality.DEVICE_REPORT)
    )
    circuits = tuple(
        Circuit(
            light_id=light.id,
            relay_channel_id=light.power_channel,
            relay_topic=f"zigbee2mqtt/relay_dev_{light.power_channel}/set",
            led_topic=f"zigbee2mqtt/light_dev_{light.id}/set",
            relay_resource=f"relay:{light.power_channel}",
            led_resource=f"led:{light.id}",
            relay_state_key=next(
                rc.state_key
                for rc in config.relay_channels
                if rc.id == light.power_channel
            ),
        )
        for light in config.lights
    )
    groups = tuple(
        GroupCircuit(
            group_id=g.id,
            topic=f"zigbee2mqtt/{g.name}_orchestrator/set",
            resource=f"group:{g.id}",
            members=g.members,
        )
        for g in config.groups
    )
    policy = PolicyConfig(
        min_send_interval_ms=100,
        publication_timeout_ms=3000,
        boot_grace_ms=1000,
        readiness_timeout_ms=5000,
    )
    reconciler = Reconciler(
        circuits=circuits,
        groups=groups,
        desired=desired,
        observations=observations,
        transport=transport,
        clock=clock,
        policy=policy,
        enabled=True,
    )
    coordinator = GroupCoordinator(
        engine=reconciler,
        clock=clock,
        barrier_ms=6000,
        members={g.id: g.members for g in config.groups},
    )

    try:
        # Submit command only to Group 1
        coordinator.submit(
            "group_1",
            IntentPatch(on=True, brightness=200, color_temp_kelvin=3000),
            source="ui",
        )

        # Check: Group 1's 10 lights have desired.on = True
        g1_members = config.groups[0].members
        for lid in g1_members:
            snap = reconciler.snapshot(lid)
            assert snap.desired.on is True
            assert snap.desired.brightness == 200
            assert snap.desired.color_temp_kelvin == 3000

        # Check: Group 2..5 lights have desired.on = False (completely independent)
        for g in config.groups[1:]:
            for lid in g.members:
                snap = reconciler.snapshot(lid)
                assert snap.desired.on is False

        # Group snapshot reflects membership count
        g1_snap = coordinator.snapshot("group_1")
        assert g1_snap.member_count == 10
        assert g1_snap.on is True

        g2_snap = coordinator.snapshot("group_2")
        assert g2_snap.member_count == 10
        assert g2_snap.on is False
        await clock.advance(0.01)
        for channel in config.relay_channels:
            reconciler.notify(
                observations.ingest(
                    RelayObservation(
                        channel.id,
                        SwitchState.ON,
                        clock.now(),
                        False,
                        EvidenceQuality.DEVICE_REPORT,
                    )
                )
            )
        for group in config.groups[1:]:
            coordinator.submit(group.id, IntentPatch(on=True), source="ui")
        await clock.advance(0.01)
        for light in config.lights[10:]:
            reconciler.notify(
                observations.ingest(
                    LedObservation(
                        light.id,
                        SwitchState.OFF,
                        clock.now(),
                        False,
                        EvidenceQuality.DEVICE_REPORT,
                    )
                )
            )
        await clock.advance(3)
        # The first ten controllers never report readiness; the other groups
        # must each publish independently while their neighbour is still booting.
        assert {item.topic for item in transport.sent} == {
            "zigbee2mqtt/Room 2_orchestrator/set",
            "zigbee2mqtt/Room 3_orchestrator/set",
            "zigbee2mqtt/Room 4_orchestrator/set",
            "zigbee2mqtt/Room 5_orchestrator/set",
        }
        assert len(transport.sent) == 4
    finally:
        await coordinator.close()
        await reconciler.close()


def test_single_gang_relay_state_key_accepted():
    config_dict = {
        "base_topic": "zigbee2mqtt",
        "relay_channels": [
            {
                "id": "relay_1",
                "device_ieee": "0x0000000000000001",
                "endpoint": 1,
                "state_key": "state",
            }
        ],
        "lights": [
            {
                "id": "light_1",
                "name": "Single Gang LED",
                "device_ieee": "0x0000000000000002",
                "endpoint": 1,
                "power_channel": "relay_1",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            }
        ],
        "groups": [],
        "rooms": [],
        "policies": {"min_send_interval_ms": 100, "publication_timeout_ms": 3000},
    }
    validated = validate_config(config_dict)
    assert validated.relay_channels[0].state_key == "state"
