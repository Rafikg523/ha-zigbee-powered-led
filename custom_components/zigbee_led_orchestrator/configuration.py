"""Validate configuration without requiring Home Assistant."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import fields

from .group_provisioner import SUFFIX, normalize_group_name, z2m_group_name
from .models import (
    GroupConfig,
    LightConfig,
    OrchestratorConfig,
    PolicyConfig,
    RelayChannelConfig,
    RoomConfig,
)

_IEEE = re.compile(r"0x[0-9a-fA-F]{16}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_STATE_KEY = re.compile(r"state(_[A-Za-z0-9_]+)?\Z")


def kelvin_to_mired(kelvin: int) -> int:
    """Convert a positive integer temperature in kelvin to rounded mireds."""
    if type(kelvin) is not int or kelvin <= 0:
        raise ValueError("kelvin must be a positive integer")
    return round(1_000_000 / kelvin)


def _mapping(value: object, path: str, allowed: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")  # noqa: TRY004
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise ValueError(f"{path}.{key}: unknown field")
    return value


def _sequence(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")  # noqa: TRY004
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or not value.strip() == value:
        raise ValueError(
            f"{path} must be a nonempty string without surrounding whitespace"
        )
    return value


def _id(value: object, path: str) -> str:
    result = _string(value, path)
    if not _ID.fullmatch(result):
        raise ValueError(f"{path} contains invalid characters")
    return result


def _ieee(value: object, path: str) -> str:
    result = _string(value, path)
    if not _IEEE.fullmatch(result):
        raise ValueError(f"{path} must be a 0x-prefixed IEEE address")
    return result.lower()


def _integer(
    value: object, path: str, *, minimum: int = 1, maximum: int | None = None
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{path} must be an integer in the allowed range")
    return value


def _unique(values: list[object], path: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{path} contains a duplicate")


def circuit_label(light_id: str, name: object = None, *, path: str = "name") -> str:
    """Return the virtual entity label, or the historical title of its id."""
    if name is None:
        return light_id.replace("_", " ").title()
    if not isinstance(name, str) or not name or name.strip() != name:
        raise ValueError(
            f"{path} must be a nonempty string without surrounding whitespace"
        )
    return name


def circuit_entity_name(value: object, *, path: str = "name") -> str:
    """Return a new circuit name ending with ``_orchestrator`` exactly once."""
    label = circuit_label("circuit", value, path=path)
    base = label[: -len(SUFFIX)].rstrip() if label.endswith(SUFFIX) else label
    if not base:
        raise ValueError(
            f"{path} must be a nonempty string without surrounding whitespace"
        )
    return f"{base}{SUFFIX}"


def validate_config(data: Mapping[str, object]) -> OrchestratorConfig:
    """Build a fully validated immutable configuration, or raise ValueError."""
    root = _mapping(
        data,
        "configuration",
        {
            "base_topic",
            "relay_channels",
            "lights",
            "groups",
            "rooms",
            "policies",
            "report_extension",
        },
    )
    base_topic = _string(root.get("base_topic"), "base_topic")
    if (
        base_topic.startswith("/")
        or base_topic.endswith("/")
        or any(c in base_topic for c in "+#\x00")
    ):
        raise ValueError("base_topic contains invalid MQTT topic characters")

    channels = []
    for index, item in enumerate(
        _sequence(root.get("relay_channels"), "relay_channels")
    ):
        path = f"relay_channels[{index}]"
        item = _mapping(item, path, {"id", "device_ieee", "endpoint", "state_key"})
        state_key = _string(item.get("state_key"), path + ".state_key")
        if not _STATE_KEY.fullmatch(state_key):
            raise ValueError(f"{path}.state_key is invalid")
        channels.append(
            RelayChannelConfig(
                id=_id(item.get("id"), path + ".id"),
                device_ieee=_ieee(item.get("device_ieee"), path + ".device_ieee"),
                endpoint=_integer(
                    item.get("endpoint"), path + ".endpoint", maximum=240
                ),
                state_key=state_key,
            )
        )
    _unique([c.id for c in channels], "relay_channels.id")
    _unique(
        [(c.device_ieee, c.state_key) for c in channels], "relay_channels.state_key"
    )
    channel_ids = {c.id for c in channels}

    lights = []
    for index, item in enumerate(_sequence(root.get("lights"), "lights")):
        path = f"lights[{index}]"
        item = _mapping(
            item,
            path,
            {
                "id",
                "name",
                "device_ieee",
                "endpoint",
                "power_channel",
                "min_kelvin",
                "max_kelvin",
            },
        )
        channel = _id(item.get("power_channel"), path + ".power_channel")
        if channel not in channel_ids:
            raise ValueError(f"{path}.power_channel is unknown")
        minimum = _integer(item.get("min_kelvin"), path + ".min_kelvin")
        maximum = _integer(item.get("max_kelvin"), path + ".max_kelvin")
        if minimum > maximum:
            raise ValueError(f"{path}.min_kelvin exceeds max_kelvin")
        light_id = _id(item.get("id"), path + ".id")
        lights.append(
            LightConfig(
                id=light_id,
                device_ieee=_ieee(item.get("device_ieee"), path + ".device_ieee"),
                endpoint=_integer(
                    item.get("endpoint"), path + ".endpoint", maximum=240
                ),
                power_channel=channel,
                min_kelvin=minimum,
                max_kelvin=maximum,
                name=circuit_label(light_id, item.get("name"), path=path + ".name"),
            )
        )
    _unique([light.id for light in lights], "lights.id")
    _unique([light.name for light in lights], "lights.name")
    _unique(
        [(light.device_ieee, light.endpoint) for light in lights],
        "lights.device_ieee/endpoint",
    )
    _unique([light.power_channel for light in lights], "lights.power_channel")
    light_ids = {light.id for light in lights}

    groups = []
    grouped: set[str] = set()
    for index, item in enumerate(_sequence(root.get("groups", []), "groups")):
        path = f"groups[{index}]"
        item = _mapping(item, path, {"id", "z2m_id", "members", "name"})
        members = [
            _id(m, path + ".members")
            for m in _sequence(item.get("members"), path + ".members")
        ]
        _unique(members, path + ".members")
        if not members or not set(members) <= light_ids:
            raise ValueError(f"{path}.members are empty or unknown")
        if grouped.intersection(members):
            raise ValueError(f"{path}.members overlap another group")
        grouped.update(members)
        group_id = _id(item.get("id"), path + ".id")
        raw_z2m_id = item.get("z2m_id")
        try:
            name = normalize_group_name(item.get("name", group_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}.name is invalid") from exc
        groups.append(
            GroupConfig(
                id=group_id,
                z2m_id=(
                    None
                    if raw_z2m_id is None
                    else _integer(raw_z2m_id, path + ".z2m_id", maximum=65535)
                ),
                members=tuple(members),
                name=name,
            )
        )
    _unique([group.id for group in groups], "groups.id")
    _unique(
        [group.z2m_id for group in groups if group.z2m_id is not None],
        "groups.z2m_id",
    )
    _unique([z2m_group_name(group.name) for group in groups], "groups.name")
    group_ids = {group.id for group in groups}

    rooms = []
    for index, item in enumerate(_sequence(root.get("rooms", []), "rooms")):
        path = f"rooms[{index}]"
        item = _mapping(item, path, {"id", "groups"})
        room_groups = [
            _id(g, path + ".groups")
            for g in _sequence(item.get("groups"), path + ".groups")
        ]
        _unique(room_groups, path + ".groups")
        if not room_groups or not set(room_groups) <= group_ids:
            raise ValueError(f"{path}.groups are empty or unknown")
        rooms.append(
            RoomConfig(id=_id(item.get("id"), path + ".id"), groups=tuple(room_groups))
        )
    _unique([room.id for room in rooms], "rooms.id")

    policy_fields = {field.name for field in fields(PolicyConfig)}
    raw_policy = _mapping(root.get("policies"), "policies", policy_fields)
    policies: dict[str, object] = {}
    for key, value in raw_policy.items():
        if key == "restore_parameters":
            if type(value) is not bool:
                raise ValueError("policies.restore_parameters must be boolean")
            policies[key] = value
            continue
        if key in {"startup_restore_power", "z2m_set_retain"}:
            if value is not False:
                raise ValueError(f"policies.{key} must be false")
            policies[key] = False
        else:
            policies[key] = _integer(
                value,
                f"policies.{key}",
                minimum=0 if key == "slider_coalesce_ms" else 1,
            )
    for key in ("min_send_interval_ms", "publication_timeout_ms"):
        if key not in policies:
            raise ValueError(f"policies.{key} is required")
    if type(root.get("report_extension", False)) is not bool:
        raise ValueError("report_extension must be boolean")
    return OrchestratorConfig(
        base_topic=base_topic,
        relay_channels=tuple(channels),
        lights=tuple(lights),
        groups=tuple(groups),
        rooms=tuple(rooms),
        policies=PolicyConfig(**policies),
        report_extension=root.get("report_extension", False),
    )
