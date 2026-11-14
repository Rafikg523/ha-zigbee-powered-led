"""Plan Zigbee2MQTT group maintenance without publishing or remembering power.

The integration owns only groups whose friendly name ends with ``_orchestrator``.
Other groups are never adopted, renamed, or modified. A planned request is not
a device command and must not be retained.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import GroupConfig

SUFFIX = "_orchestrator"
MIN_GROUP_ID = 1
MAX_GROUP_ID = 254


def normalize_group_name(value: object) -> str:
    """Return the user's label without the orchestrator suffix."""
    if not isinstance(value, str):
        raise TypeError("group name must be a string")
    name = value.strip()
    if name.endswith(SUFFIX):
        name = name[: -len(SUFFIX)].rstrip()
    if not name or len(name) > 48:
        raise ValueError("group name length is invalid")
    if any(char in name for char in "+#\x00/"):
        raise ValueError("group name contains invalid characters")
    if not all(char.isalnum() or char in " _-" for char in name):
        raise ValueError("group name contains invalid characters")
    if name.lower() == "bridge":
        raise ValueError("group name is reserved")
    return name


def z2m_group_name(name: str) -> str:
    """Friendly name shared by the Z2M group and its Home Assistant entity."""
    base = normalize_group_name(name)
    return f"{base}{SUFFIX}"


@dataclass(frozen=True, slots=True)
class CatalogGroup:
    z2m_id: int
    name: str
    members: frozenset[tuple[str, int]]


@dataclass(frozen=True, slots=True)
class LightEndpoint:
    ieee: str
    endpoint: int
    friendly_name: str


@dataclass(frozen=True, slots=True)
class BridgeAction:
    """One bridge request. Paths never include ``remove_all``."""

    path: str
    payload: dict[str, object]


def options_confirmed(response: object) -> bool:
    """True only when Z2M reports the group will not invent or retain state."""
    if not isinstance(response, dict) or response.get("status") != "ok":
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    updated = data.get("to")
    if not isinstance(updated, dict):
        return False
    return updated.get("optimistic") is False and updated.get("retain") is False


def stored_group_ids(groups: object, group_id: str, z2m_id: int) -> list | None:
    """Return group entries with one Zigbee id replaced, or None if unchanged."""
    if not isinstance(groups, list) or type(z2m_id) is not int:
        raise TypeError("invalid group binding")
    changed = False
    updated = []
    for item in groups:
        if not isinstance(item, dict):
            raise TypeError("invalid group binding")
        current = dict(item)
        if current.get("id") == group_id and current.get("z2m_id") != z2m_id:
            current["z2m_id"] = z2m_id
            changed = True
        updated.append(current)
    return updated if changed else None


def _expected(group: GroupConfig, lights: Mapping[str, LightEndpoint]):
    return frozenset(
        (lights[item].ieee.lower(), lights[item].endpoint) for item in group.members
    )


def _free_id(used: set[int]) -> int:
    for candidate in range(MIN_GROUP_ID, MAX_GROUP_ID + 1):
        if candidate not in used:
            return candidate
    raise ValueError("no free zigbee group id")


def _one(
    group: GroupConfig,
    lights: Mapping[str, LightEndpoint],
    catalog: tuple[CatalogGroup, ...],
    powered: frozenset[str],
    options_ready: frozenset[str],
    pending: Mapping[str, int],
) -> BridgeAction | None:
    friendly = z2m_group_name(group.name)
    expected = _expected(group, lights)
    owned = [item for item in catalog if item.name == friendly]
    if len(owned) > 1:
        return None
    if not owned:
        if friendly not in pending and friendly not in options_ready:
            used = {item.z2m_id for item in catalog}
            used.update(pending.values())
            return BridgeAction(
                "group/add", {"friendly_name": friendly, "id": _free_id(used)}
            )
        if friendly not in options_ready:
            return BridgeAction(
                "group/options",
                {"id": friendly, "options": {"optimistic": False, "retain": False}},
            )
        return None
    if friendly not in options_ready:
        return BridgeAction(
            "group/options",
            {"id": friendly, "options": {"optimistic": False, "retain": False}},
        )
    current = owned[0]
    for light_id in group.members:
        endpoint = (lights[light_id].ieee.lower(), lights[light_id].endpoint)
        if endpoint in current.members or light_id not in powered:
            continue
        light = lights[light_id]
        return BridgeAction(
            "group/members/add",
            {
                "group": friendly,
                "device": light.friendly_name,
                "endpoint": light.endpoint,
            },
        )
    for ieee, endpoint in sorted(current.members - expected):
        powered_owners = [
            lights[light_id]
            for light_id in group.members
            if lights[light_id].ieee.lower() == ieee and light_id in powered
        ]
        if not powered_owners:
            continue
        return BridgeAction(
            "group/members/remove",
            {
                "group": friendly,
                "device": powered_owners[0].friendly_name,
                "endpoint": endpoint,
                "skip_disable_reporting": True,
            },
        )
    return None


def next_group_action(
    groups: tuple[GroupConfig, ...],
    lights: Mapping[str, LightEndpoint],
    catalog: tuple[CatalogGroup, ...],
    powered: frozenset[str],
    options_ready: frozenset[str],
    pending: Mapping[str, int] | None = None,
) -> BridgeAction | None:
    """Return the next idempotent bridge request, or None when nothing is safe."""
    waiting = pending or {}
    for group in groups:
        if not set(group.members) <= set(lights):
            return None
        action = _one(group, lights, catalog, powered, options_ready, waiting)
        if action is not None:
            return action
    return None
