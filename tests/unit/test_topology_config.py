"""Synthetic config must match the observed topology before use."""

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.topology import Topology
from tests.unit.test_configuration import valid_config


def topology():
    result = Topology("zigbee2mqtt")
    result.update_devices(
        [
            {
                "ieee_address": "0x0000000000000001",
                "friendly_name": "relay",
                "endpoints": {"1": {"name": "l1"}, "2": {"name": "l2"}},
                "definition": {
                    "exposes": [
                        {
                            "type": "switch",
                            "endpoint": "l1",
                            "features": [
                                {
                                    "name": "state",
                                    "property": "state_l1",
                                    "access": 7,
                                    "value_on": "ON",
                                    "value_off": "OFF",
                                }
                            ],
                        },
                        {
                            "type": "switch",
                            "endpoint": "l2",
                            "features": [
                                {
                                    "name": "state",
                                    "property": "state_l2",
                                    "access": 7,
                                    "value_on": "ON",
                                    "value_off": "OFF",
                                }
                            ],
                        },
                    ]
                },
            },
            {
                "ieee_address": "0x0000000000000002",
                "friendly_name": "led1",
                "endpoints": {"1": {}},
                "definition": {
                    "exposes": [
                        {
                            "type": "light",
                            "features": [
                                {
                                    "name": "state",
                                    "property": "state",
                                    "access": 7,
                                    "value_on": "ON",
                                    "value_off": "OFF",
                                }
                            ],
                        }
                    ]
                },
            },
            {
                "ieee_address": "0x0000000000000003",
                "friendly_name": "led2",
                "endpoints": {"1": {}},
                "definition": {
                    "exposes": [
                        {
                            "type": "light",
                            "features": [
                                {
                                    "name": "state",
                                    "property": "state",
                                    "access": 7,
                                    "value_on": "ON",
                                    "value_off": "OFF",
                                }
                            ],
                        }
                    ]
                },
            },
        ]
    )
    result.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                    {"ieee_address": "0x0000000000000003", "endpoint": 1},
                ],
            }
        ]
    )
    return result


def test_config_checks_devices_endpoints_and_group_membership():
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    config = validate_config(valid_config())
    catalog = topology()
    validate_topology(config, catalog)
    catalog.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                ],
            }
        ]
    )
    with pytest.raises(ValueError, match="members"):
        validate_topology(config, catalog)


def test_missing_endpoint_is_rejected():
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    raw = valid_config()
    raw["lights"][0]["endpoint"] = 2
    with pytest.raises(ValueError, match="endpoint"):
        validate_topology(validate_config(raw), topology())


def test_group_with_relay_endpoints_is_rejected():
    """A group including both relay channels is not a two-light group."""
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    catalog = topology()
    catalog.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                    {"ieee_address": "0x0000000000000003", "endpoint": 1},
                    {"ieee_address": "0x0000000000000001", "endpoint": 1},
                    {"ieee_address": "0x0000000000000001", "endpoint": 2},
                ],
            }
        ]
    )
    with pytest.raises(ValueError, match="group members differ"):
        validate_topology(validate_config(valid_config()), catalog)


def test_relay_state_key_must_be_verified_for_its_endpoint():
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    raw = valid_config()
    raw["relay_channels"][0]["state_key"] = "state_wrong"
    with pytest.raises(ValueError, match="state key"):
        validate_topology(validate_config(raw), topology())


def test_relay_values_must_match_explicit_on_off_contract():
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    catalog = topology()
    device = catalog.resolve_device("0x0000000000000001")
    exposes = device.capabilities
    exposes[0]["features"][0]["value_on"] = "OPEN"
    entries = [
        {
            "ieee_address": "0x0000000000000001",
            "friendly_name": "relay",
            "endpoints": {"1": {"name": "l1"}, "2": {"name": "l2"}},
            "definition": {"exposes": exposes},
        },
        {
            "ieee_address": "0x0000000000000002",
            "friendly_name": "led1",
            "endpoints": {"1": {}},
            "definition": None,
        },
        {
            "ieee_address": "0x0000000000000003",
            "friendly_name": "led2",
            "endpoints": {"1": {}},
            "definition": None,
        },
    ]
    catalog.update_devices(entries)
    catalog.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                    {"ieee_address": "0x0000000000000003", "endpoint": 1},
                ],
            }
        ]
    )
    with pytest.raises(ValueError, match="state key"):
        validate_topology(validate_config(valid_config()), catalog)


def test_led_endpoint_must_expose_on_off_control():
    from custom_components.zigbee_led_orchestrator.topology import validate_topology

    catalog = topology()
    device = catalog.resolve_device("0x0000000000000002")
    assert device.capabilities
    bad = device.capabilities
    bad[0]["features"][0]["value_on"] = "OPEN"
    entries = [
        {
            "ieee_address": item.ieee,
            "friendly_name": item.topic.removeprefix("zigbee2mqtt/"),
            "endpoints": {
                str(endpoint): {"name": item.endpoint_name(endpoint)}
                for endpoint in item.endpoints
            },
            "definition": {
                "exposes": bad if item.ieee == device.ieee else item.capabilities
            },
        }
        for item in catalog.devices()
    ]
    catalog.update_devices(entries)
    catalog.update_groups(
        [
            {
                "id": 1,
                "friendly_name": "group_a_orchestrator",
                "members": [
                    {"ieee_address": "0x0000000000000002", "endpoint": 1},
                    {"ieee_address": "0x0000000000000003", "endpoint": 1},
                ],
            }
        ]
    )
    with pytest.raises(ValueError, match="light state"):
        validate_topology(validate_config(valid_config()), catalog)
