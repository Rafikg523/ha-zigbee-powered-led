"""Group provisioning plans stay away from foreign groups and remembered power."""

import pytest

from custom_components.zigbee_led_orchestrator.configuration import validate_config
from custom_components.zigbee_led_orchestrator.group_provisioner import (
    CatalogGroup,
    LightEndpoint,
    next_group_action,
    normalize_group_name,
    options_confirmed,
    stored_group_ids,
    z2m_group_name,
)
from tests.unit.test_configuration import valid_config

LIGHTS = {
    "light_a": LightEndpoint("0x0000000000000002", 1, "room/led1"),
    "light_b": LightEndpoint("0x0000000000000003", 1, "room/led2"),
}
FOREIGN = CatalogGroup(
    1,
    "room/group",
    frozenset({("0x0000000000000002", 1), ("0x0000000000000003", 1)}),
)


def groups(name="Salon"):
    config = valid_config()
    config["groups"][0]["name"] = name
    config["groups"][0].pop("z2m_id")
    return validate_config(config).groups


def test_custom_name_is_used_for_the_entity_and_the_z2m_group():
    assert normalize_group_name("  Salon_orchestrator  ") == "Salon"
    assert z2m_group_name("Salon") == "Salon_orchestrator"
    with pytest.raises(ValueError):
        normalize_group_name("bridge")
    with pytest.raises(ValueError):
        normalize_group_name("room/group")


def test_missing_group_is_created_without_touching_a_user_group():
    action = next_group_action(groups(), LIGHTS, (FOREIGN,), frozenset(), frozenset())
    assert action.path == "group/add"
    assert action.payload == {"friendly_name": "Salon_orchestrator", "id": 2}
    assert "remove_all" not in action.path
    assert "room/group" not in action.payload.values()


def test_options_disable_optimistic_and_retained_group_state():
    catalog = (
        FOREIGN,
        CatalogGroup(2, "Salon_orchestrator", frozenset()),
    )
    action = next_group_action(groups(), LIGHTS, catalog, frozenset(), frozenset())
    assert action.path == "group/options"
    assert action.payload["options"] == {"optimistic": False, "retain": False}
    assert options_confirmed(
        {"status": "ok", "data": {"to": {"optimistic": False, "retain": False}}}
    )
    assert not options_confirmed(
        {"status": "ok", "data": {"to": {"optimistic": True, "retain": False}}}
    )


def test_members_are_added_only_while_powered_and_removed_without_disable_all():
    catalog = (CatalogGroup(2, "Salon_orchestrator", frozenset()),)
    ready = frozenset({"Salon_orchestrator"})
    assert next_group_action(groups(), LIGHTS, catalog, frozenset(), ready) is None
    added = next_group_action(groups(), LIGHTS, catalog, frozenset({"light_a"}), ready)
    assert added.path == "group/members/add"
    assert added.payload == {
        "group": "Salon_orchestrator",
        "device": "room/led1",
        "endpoint": 1,
    }
    extra = CatalogGroup(
        2,
        "Salon_orchestrator",
        frozenset({("0x0000000000000002", 1), ("0x0000000000000002", 2)}),
    )
    removed = next_group_action(
        groups(), LIGHTS, (extra,), frozenset({"light_a"}), ready
    )
    assert removed.path == "group/members/remove"
    assert removed.payload["skip_disable_reporting"] is True
    assert removed.payload["endpoint"] == 2
    assert "remove_all" not in removed.path


def test_unpowered_extra_member_is_left_in_place():
    extra = CatalogGroup(
        2,
        "Salon_orchestrator",
        frozenset({("0x0000000000000003", 1), ("0x0000000000000009", 1)}),
    )
    action = next_group_action(
        groups(),
        LIGHTS,
        (extra,),
        frozenset(),
        frozenset({"Salon_orchestrator"}),
    )
    assert action is None


def test_circuit_taken_out_of_the_group_leaves_it_once_powered():
    lights = dict(LIGHTS, light_c=LightEndpoint("0x0000000000000004", 1, "room/led3"))
    catalog = (
        CatalogGroup(
            2,
            "Salon_orchestrator",
            frozenset(
                {
                    ("0x0000000000000002", 1),
                    ("0x0000000000000003", 1),
                    ("0x0000000000000004", 1),
                }
            ),
        ),
    )
    ready = frozenset({"Salon_orchestrator"})
    assert next_group_action(groups(), lights, catalog, frozenset(), ready) is None
    removed = next_group_action(
        groups(), lights, catalog, frozenset({"light_c"}), ready
    )
    assert removed.path == "group/members/remove"
    assert removed.payload == {
        "group": "Salon_orchestrator",
        "device": "room/led3",
        "endpoint": 1,
        "skip_disable_reporting": True,
    }


def test_stored_binding_updates_only_the_orchestrator_id():
    original = [
        {"id": "group_a", "z2m_id": 1, "name": "Salon", "members": ["light_a"]},
        {"id": "other", "z2m_id": 4, "members": ["light_b"]},
    ]
    assert stored_group_ids(original, "group_a", 1) is None
    updated = stored_group_ids(original, "group_a", 2)
    assert updated[0]["z2m_id"] == 2
    assert updated[1] == original[1]
    assert original[0]["z2m_id"] == 1
