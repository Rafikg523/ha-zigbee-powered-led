"""Conservative, Home Assistant independent Zigbee2MQTT device catalog."""

import json
import re
from dataclasses import dataclass

from .group_provisioner import z2m_group_name


@dataclass(frozen=True, slots=True)
class Device:
    ieee: str
    topic: str
    endpoints: frozenset[int]
    _capabilities_json: str
    _endpoint_names: tuple[tuple[int, str | None], ...]
    name: str

    def endpoint_name(self, endpoint: int) -> str | None:
        return dict(self._endpoint_names)[endpoint]

    @property
    def capabilities(self) -> list[object]:
        """Return a defensive copy of Z2M exposes."""
        return json.loads(self._capabilities_json)


@dataclass(frozen=True, slots=True)
class Group:
    topic: str
    members: frozenset[tuple[str, int]]
    name: str


class Topology:
    """Hold validated snapshots and revoke stale addresses on invalid input."""

    def __init__(self, base_topic: str) -> None:
        self._base_topic = base_topic
        self._devices: dict[str, Device] = {}
        self._groups: dict[int, Group] = {}
        self.groups_seen = False
        self._name_owners: dict[str, str] = {}
        self._group_name_owners: dict[str, int] = {}
        self.version = 0

    def update_devices(self, entries: object) -> None:
        self.version += 1
        # Parsing replaces the device table. Hold the previous groups until the
        # new table is valid so an identical republish does not look like a
        # membership change. A failed parse still leaves both tables empty.
        previous_groups = self._groups
        previous_seen = self.groups_seen
        self._devices = {}
        self._groups = {}
        self.groups_seen = False
        if not isinstance(entries, list):
            raise TypeError("devices must be a list")
        devices: dict[str, Device] = {}
        names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("device must be an object")
            ieee = entry.get("ieee_address")
            name = entry.get("friendly_name")
            if not isinstance(ieee, str) or not re.fullmatch(
                r"0x[0-9a-fA-F]{16}", ieee
            ):
                raise ValueError("invalid IEEE address")
            if (
                not isinstance(name, str)
                or not name
                or any(part == "" for part in name.split("/"))
                or name.split("/")[0] == "bridge"
                or "+" in name
                or "#" in name
                or "\x00" in name
            ):
                raise ValueError("invalid friendly name")
            ieee = ieee.lower()
            if (
                ieee in devices
                or name in names
                or name in self._group_name_owners
                or (name in self._name_owners and self._name_owners[name] != ieee)
            ):
                raise ValueError("duplicate device or name")
            names.add(name)
            endpoints = entry.get("endpoints", {})
            if not isinstance(endpoints, dict) or any(
                not isinstance(key, str) or not key.isascii() or not key.isdecimal()
                for key in endpoints
            ):
                raise ValueError("invalid endpoints")
            endpoint_names = []
            for key, detail in endpoints.items():
                if not isinstance(detail, dict):
                    raise TypeError("invalid endpoint metadata")
                endpoint_name = detail.get("name")
                if endpoint_name is not None and (
                    not isinstance(endpoint_name, str) or not endpoint_name
                ):
                    raise ValueError("invalid endpoint name")
                endpoint_names.append((int(key), endpoint_name))
            definition = entry.get("definition")
            if definition is not None and not isinstance(definition, dict):
                raise ValueError("invalid device definition")
            exposes = definition.get("exposes", []) if definition is not None else []
            if not isinstance(exposes, list):
                raise TypeError("invalid exposes")
            try:
                capabilities_json = json.dumps(exposes, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid capabilities") from exc
            devices[ieee] = Device(
                ieee,
                f"{self._base_topic}/{name}",
                frozenset(int(key) for key in endpoints),
                capabilities_json,
                tuple(endpoint_names),
                name,
            )
        self._devices = devices
        self._name_owners.update(
            {d.topic[len(self._base_topic) + 1 :]: d.ieee for d in devices.values()}
        )
        restored = {
            group_id: group
            for group_id, group in previous_groups.items()
            if all(
                ieee in devices and endpoint in devices[ieee].endpoints
                for ieee, endpoint in group.members
            )
        }
        if restored:
            self._groups = restored
            self.groups_seen = previous_seen

    def devices(self) -> tuple[Device, ...]:
        return tuple(self._devices.values())

    def groups(self) -> tuple[tuple[int, Group], ...]:
        return tuple(self._groups.items())

    def resolve_device(self, ieee: str) -> Device:
        return self._devices[ieee.lower()]

    def update_groups(self, entries: object, *, catalog: bool = True) -> None:
        self.version += 1
        self._groups = {}
        self.groups_seen = False
        if not isinstance(entries, list):
            raise TypeError("groups must be a list")
        groups: dict[int, Group] = {}
        names = {device.topic for device in self._devices.values()}
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("group must be an object")
            group_id, name, members = (
                entry.get("id"),
                entry.get("friendly_name"),
                entry.get("members"),
            )
            if (
                type(group_id) is not int
                or not 1 <= group_id <= 65535
                or group_id in groups
            ):
                raise ValueError("invalid group id")
            if (
                not isinstance(name, str)
                or not name
                or any(not part for part in name.split("/"))
                or name.split("/")[0] == "bridge"
                or any(c in name for c in "+#\x00")
            ):
                raise ValueError("invalid group name")
            topic = f"{self._base_topic}/{name}"
            if (
                topic in names
                or name in self._name_owners
                or (
                    name in self._group_name_owners
                    and self._group_name_owners[name] != group_id
                )
                or not isinstance(members, list)
            ):
                raise ValueError("duplicate group name or invalid members")
            names.add(topic)
            validated: set[tuple[str, int]] = set()
            for member in members:
                if not isinstance(member, dict):
                    raise TypeError("invalid member")
                ieee, endpoint = member.get("ieee_address"), member.get("endpoint")
                if not isinstance(ieee, str) or type(endpoint) is not int:
                    raise ValueError("invalid member")
                ieee = ieee.lower()
                if (
                    ieee not in self._devices
                    or endpoint not in self._devices[ieee].endpoints
                ):
                    raise ValueError("unknown member endpoint")
                if (ieee, endpoint) in validated:
                    raise ValueError("duplicate member")
                validated.add((ieee, endpoint))
            groups[group_id] = Group(topic, frozenset(validated), name)
        self._groups = groups
        self.groups_seen = catalog
        self._group_name_owners.update(
            {
                group.topic[len(self._base_topic) + 1 :]: group_id
                for group_id, group in groups.items()
            }
        )

    def resolve_group(self, group_id: int) -> Group:
        return self._groups[group_id]

    def group_named(self, name: str) -> tuple[int, Group] | None:
        found = [
            (group_id, group)
            for group_id, group in self._groups.items()
            if group.name == name
        ]
        if len(found) != 1:
            return None
        return found[0]


def validate_topology(
    config, topology: Topology, *, require_groups: bool = True
) -> None:
    """Require every configured address.

    Group membership is required only when ``require_groups`` is true. A missing
    or different orchestrator group must not block individual circuits; it only
    withholds the native group command.
    """
    for item in (*config.relay_channels, *config.lights):
        try:
            device = topology.resolve_device(item.device_ieee)
        except KeyError as exc:
            raise ValueError("configured device missing from topology") from exc
        if item.endpoint not in device.endpoints:
            raise ValueError("configured endpoint missing from topology")
    for channel in config.relay_channels:
        device = topology.resolve_device(channel.device_ieee)
        endpoint_name = device.endpoint_name(channel.endpoint)
        found = False
        for expose in device.capabilities:
            if not isinstance(expose, dict) or expose.get("type") != "switch":
                continue
            if expose.get("endpoint") != endpoint_name:
                continue
            features = expose.get("features", [])
            if not isinstance(features, list):
                continue
            found |= any(
                isinstance(feature, dict)
                and feature.get("property") == channel.state_key
                and feature.get("value_on") == "ON"
                and feature.get("value_off") == "OFF"
                and type(feature.get("access")) is int
                and feature["access"] & 2 != 0
                for feature in features
            )
        if not found:
            raise ValueError("configured relay state key is not verified for endpoint")
    for light in config.lights:
        device = topology.resolve_device(light.device_ieee)
        endpoint_name = device.endpoint_name(light.endpoint)
        supported = False
        for expose in device.capabilities:
            if (
                not isinstance(expose, dict)
                or expose.get("type") != "light"
                or expose.get("endpoint") != endpoint_name
            ):
                continue
            features = expose.get("features", [])
            if not isinstance(features, list):
                continue
            supported |= any(
                isinstance(feature, dict)
                and feature.get("property") == "state"
                and feature.get("value_on") == "ON"
                and feature.get("value_off") == "OFF"
                and type(feature.get("access")) is int
                and feature["access"] & 2 != 0
                for feature in features
            )
        if not supported:
            raise ValueError("configured light state is not verified for endpoint")
    lights = {light.id: light for light in config.lights}
    for group in config.groups:
        found = topology.group_named(z2m_group_name(group.name))
        expected = frozenset(
            (lights[light].device_ieee.lower(), lights[light].endpoint)
            for light in group.members
        )
        if not require_groups:
            continue
        if found is None:
            raise ValueError("configured group missing from topology")
        if found[1].members != expected:
            raise ValueError("configured group members differ from topology")
