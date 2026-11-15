"""Edit or remove one stored circuit or group without Home Assistant."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from .configuration import validate_config


class CircuitInGroupError(ValueError):
    """A grouped circuit cannot be removed or remapped."""

    def __init__(self, group_name: str) -> None:
        super().__init__(f"circuit belongs to group {group_name}")
        self.group_name = group_name


def find(data: Mapping, kind: str, item_id: str) -> dict | None:
    """Return the stored light or group with ``item_id``."""
    for item in data.get(kind, []):
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


def group_of(data: Mapping, light_id: str) -> dict | None:
    for group in data.get("groups", []):
        if isinstance(group, dict) and light_id in (group.get("members") or []):
            return group
    return None


def without_circuit(data: Mapping, light_id: str) -> dict:
    """Drop one circuit and its own relay channel."""
    light = find(data, "lights", light_id)
    if light is None:
        raise KeyError(light_id)
    group = group_of(data, light_id)
    if group is not None:
        raise CircuitInGroupError(str(group.get("name", group.get("id"))))
    updated = copy.deepcopy(dict(data))
    updated["lights"] = [item for item in updated["lights"] if item["id"] != light_id]
    updated["relay_channels"] = [
        item
        for item in updated["relay_channels"]
        if item["id"] != light["power_channel"]
    ]
    validate_config(updated)
    return updated


def without_group(data: Mapping, group_id: str) -> dict:
    """Drop one group; its circuits stay configured on their own."""
    if find(data, "groups", group_id) is None:
        raise KeyError(group_id)
    updated = copy.deepcopy(dict(data))
    updated["groups"] = [item for item in updated["groups"] if item["id"] != group_id]
    updated["rooms"] = [
        room
        for room in (
            {**item, "groups": [g for g in item["groups"] if g != group_id]}
            for item in updated.get("rooms", [])
        )
        if room["groups"]
    ]
    validate_config(updated)
    return updated
