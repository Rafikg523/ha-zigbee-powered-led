"""Safe configuration flow based on an observed Zigbee2MQTT catalog."""

from __future__ import annotations

import asyncio
import copy
import json

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .configuration import circuit_label, validate_config
from .const import DOMAIN
from .group_provisioner import normalize_group_name, z2m_group_name
from .light_capabilities import LightCapabilities
from .models import ControlMode
from .topology import Topology, validate_topology

_POLICY_DEFAULTS = (
    ("min_send_interval_ms", 100),
    ("publication_timeout_ms", 3000),
    ("boot_grace_ms", 2500),
    ("readiness_timeout_ms", 8000),
)
_MENU_STEPS = ("add_circuit", "create_group", "edit_circuit", "edit_group", "control")
_MENU_LABELS = {
    "en": {
        "add_circuit": "Add circuit",
        "create_group": "Create group",
        "edit_circuit": "Edit circuit",
        "edit_group": "Edit group",
        "control": "Control mode",
    },
    "pl": {
        "add_circuit": "Dodaj obwód",
        "create_group": "Utwórz grupę",
        "edit_circuit": "Edytuj obwód",
        "edit_group": "Edytuj grupę",
        "control": "Tryb sterowania",
    },
}


def control_error(exc: Exception) -> str:
    """Map a refused activation to a specific options-flow error."""
    if isinstance(exc, AttributeError):
        return "runtime_not_running"
    if isinstance(exc, ValueError):
        return "mapping_mismatch"
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith("topology changed"):
            return "topology_stale"
        if text.startswith("configured devices"):
            return "devices_missing"
        if text.startswith("report extension"):
            return "report_extension_unavailable"
        if text.startswith("invalid") or "closed" in text:
            return "runtime_not_running"
        return "bridge_unavailable"
    return "control_unavailable"


def menu_options_for(language: str) -> dict[str, str]:
    """Return menu labels in the flow result.

    A list of step ids is labeled by the frontend from cached translations.
    When that cache predates the menu, each row renders as an empty chevron.
    A mapping is shown as-is.
    """
    code = "pl" if str(language).lower().startswith("pl") else "en"
    labels = _MENU_LABELS[code]
    return {step: labels[step] for step in _MENU_STEPS}


async def discover_topology(hass, base_topic: str) -> Topology:
    """Receive retained metadata without publishing discovery requests."""
    received = {}
    ready = asyncio.Event()
    unsubscribes = []

    @callback
    def on_message(message):
        try:
            value = json.loads(message.payload)
            if not isinstance(value, list):
                return
            received[message.topic.rsplit("/", 1)[-1]] = value
            if "devices" in received and "groups" in received:
                ready.set()
        except TypeError, ValueError:
            return

    try:
        for suffix in ("devices", "groups"):
            unsubscribes.append(
                await mqtt.async_subscribe(
                    hass, f"{base_topic}/bridge/{suffix}", on_message
                )
            )
        async with asyncio.timeout(10):
            await ready.wait()
        topology = Topology(base_topic)
        topology.update_devices(received["devices"])
        topology.update_groups(received["groups"])
        return topology
    finally:
        for unsubscribe in unsubscribes:
            unsubscribe()


def _require_name(value: object) -> str:
    if value is None:
        raise ValueError(
            "name must be a nonempty string without surrounding whitespace"
        )
    return circuit_label("circuit", value, path="name")


def _split_light(token: object) -> tuple[str, int]:
    if not isinstance(token, str):
        raise TypeError("light selection is invalid")
    ieee, endpoint = token.split("|")
    return ieee.lower(), int(endpoint)


def _split_relay(token: object) -> tuple[str, int, str]:
    if not isinstance(token, str):
        raise TypeError("relay selection is invalid")
    ieee, endpoint, key = token.split("|")
    return ieee.lower(), int(endpoint), key


def _stored_label(light: dict) -> str:
    return circuit_label(str(light["id"]), light.get("name"), path="name")


def _pair_taken(data: dict, name: str, light: tuple[str, int], relay: tuple) -> bool:
    ieee, endpoint = light
    for item in data.get("lights", []):
        if _stored_label(item) == name:
            return True
        if item["device_ieee"].lower() == ieee and int(item["endpoint"]) == endpoint:
            return True
    relay_ieee, _relay_endpoint, key = relay
    return any(
        item["device_ieee"].lower() == relay_ieee and item["state_key"] == key
        for item in data.get("relay_channels", [])
    )


def _next_ids(data: dict, retired=()) -> tuple[str, str]:
    used = {item["id"] for item in data.get("lights", [])}
    used.update(item["id"] for item in data.get("relay_channels", []))
    used.update(retired)
    index = 1
    while f"light_{index}" in used or f"relay_{index}" in used:
        index += 1
    return f"light_{index}", f"relay_{index}"


def _kelvin_range(catalog: Topology, ieee: str, endpoint: int) -> tuple[int, int]:
    device = catalog.resolve_device(ieee)
    capability = LightCapabilities.from_exposes(
        device.capabilities,
        endpoint=device.endpoint_name(endpoint),
    )
    mired = capability.mired_range
    if not mired:
        return (2000, 6500)
    return (round(1_000_000 / mired[1]), round(1_000_000 / mired[0]))


def _catalog_choices(catalog: Topology, data: dict) -> tuple[dict, dict]:
    used_lights = {
        (item["device_ieee"].lower(), int(item["endpoint"]))
        for item in data.get("lights", [])
    }
    used_relays = {
        (item["device_ieee"].lower(), item["state_key"])
        for item in data.get("relay_channels", [])
    }
    light_options = {}
    relay_options = {}
    for device in catalog.devices():
        for endpoint in device.endpoints:
            label = f"{device.topic} — {device.ieee} / {endpoint}"
            endpoint_name = device.endpoint_name(endpoint)
            if (device.ieee, endpoint) not in used_lights and any(
                isinstance(expose, dict)
                and expose.get("type") == "light"
                and expose.get("endpoint") in (endpoint_name, str(endpoint), None)
                for expose in device.capabilities
            ):
                light_options[f"{device.ieee}|{endpoint}"] = label
            for expose in device.capabilities:
                if (
                    not isinstance(expose, dict)
                    or expose.get("type") != "switch"
                    or expose.get("endpoint")
                    not in (endpoint_name, str(endpoint), None)
                ):
                    continue
                features = expose.get("features", [])
                if not isinstance(features, list):
                    continue
                for feature in features:
                    if not isinstance(feature, dict):
                        continue
                    key = feature.get("property")
                    if (
                        isinstance(key, str)
                        and (device.ieee, key) not in used_relays
                        and feature.get("value_on") == "ON"
                        and feature.get("value_off") == "OFF"
                        and type(feature.get("access")) is int
                        and feature["access"] & 2
                    ):
                        relay_options[f"{device.ieee}|{endpoint}|{key}"] = (
                            f"{label} / {key}"
                        )
    return light_options, relay_options


def _policy_schema(schema: dict) -> None:
    for key, default in _POLICY_DEFAULTS:
        schema[vol.Required(key, default=default)] = vol.All(
            vol.Coerce(int), vol.Range(min=1)
        )


def _policies_from_input(user_input: dict) -> dict:
    policies = {}
    for key, default in _POLICY_DEFAULTS:
        policies[key] = user_input.get(key, default)
    return policies


def _grouped_ids(data: dict) -> set[str]:
    grouped: set[str] = set()
    for group in data.get("groups", []):
        if isinstance(group, dict):
            grouped.update(str(item) for item in group.get("members") or [])
    return grouped


def _free_lights(data: dict) -> list[dict]:
    grouped = _grouped_ids(data)
    return [
        light
        for light in data.get("lights", [])
        if isinstance(light, dict) and light.get("id") not in grouped
    ]


def _next_group_id(data: dict, retired=()) -> str:
    used = {item.get("id") for item in data.get("groups", []) if isinstance(item, dict)}
    used.update(retired)
    index = 1
    while f"group_{index}" in used:
        index += 1
    return f"group_{index}"


def _group_name(raw: object) -> str:
    try:
        return normalize_group_name(raw)
    except (TypeError, ValueError) as exc:
        raise _FormError("invalid_group_name") from exc


def _known_group_names(data: dict) -> set[str]:
    names = set()
    for group in data.get("groups", []):
        if not isinstance(group, dict):
            continue
        try:
            names.add(z2m_group_name(normalize_group_name(group.get("name", ""))))
        except TypeError, ValueError:
            continue
    return names


class _FormError(Exception):
    """A mapping the user can correct in the same form."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _circuit_fields(catalog: Topology, user_input: dict, data: dict, retired=()):
    try:
        name = _require_name(user_input.get("name"))
    except ValueError as exc:
        raise _FormError("invalid_name") from exc
    try:
        light = _split_light(user_input["light"])
        relay = _split_relay(user_input["relay"])
        if _pair_taken(data, name, light, relay):
            raise _FormError("already_mapped")
        minimum, maximum = _kelvin_range(catalog, light[0], light[1])
    except _FormError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise _FormError("invalid_mapping") from exc
    light_id, relay_id = _next_ids(data, retired)
    return (
        name,
        light,
        relay,
        light_id,
        relay_id,
        user_input.get("min_kelvin", minimum),
        user_input.get("max_kelvin", maximum),
    )


def _circuit_records(fields) -> tuple[dict, dict]:
    name, light, relay, light_id, relay_id, minimum, maximum = fields
    channel = {
        "id": relay_id,
        "device_ieee": relay[0],
        "endpoint": relay[1],
        "state_key": relay[2],
    }
    configured_light = {
        "id": light_id,
        "name": name,
        "device_ieee": light[0],
        "endpoint": light[1],
        "power_channel": relay_id,
        "min_kelvin": minimum,
        "max_kelvin": maximum,
    }
    return channel, configured_light


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Permit one configured instance and verify all physical addresses."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance")
        errors = {}
        if user_input is not None:
            base_topic = user_input.get("base_topic", "")
            if (
                not isinstance(base_topic, str)
                or not base_topic
                or base_topic.startswith("/")
                or base_topic.endswith("/")
                or any(c in base_topic for c in "+#\x00")
            ):
                errors["base"] = "invalid_mapping"
            else:
                try:
                    await discover_topology(self.hass, base_topic)
                except TimeoutError, ValueError, TypeError, KeyError:
                    errors["base"] = "catalog_unavailable"
                else:
                    try:
                        data = {
                            "base_topic": base_topic,
                            "relay_channels": [],
                            "lights": [],
                            "groups": [],
                            "rooms": [],
                            "policies": _policies_from_input(user_input),
                            "report_extension": user_input.get(
                                "report_extension", False
                            ),
                        }
                        validate_config(data)
                    except TypeError, ValueError, KeyError:
                        errors["base"] = "invalid_mapping"
                    else:
                        if self._async_current_entries():
                            return self.async_abort(reason="single_instance")
                        return self.async_create_entry(
                            title="Zigbee LED Orchestrator", data=data
                        )
        schema = {
            vol.Required("base_topic", default="zigbee2mqtt"): str,
            vol.Optional("report_extension", default=False): bool,
        }
        _policy_schema(schema)
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema), errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Change mode or append one circuit without a second integration entry."""

    async def async_step_init(self, user_input=None):
        return await self.async_step_choose(user_input)

    async def async_step_choose(self, user_input=None):
        menu = menu_options_for(self.hass.config.language)
        if self.config_entry.options.get("pending_group_operation"):
            menu["recover_group"] = (
                "Dokończ operację grupy"
                if str(self.hass.config.language).startswith("pl")
                else "Recover group operation"
            )
        return self.async_show_menu(
            step_id="choose",
            menu_options=menu,
        )

    async def async_step_recover_group(self, user_input=None):
        from homeassistant.exceptions import HomeAssistantError

        from .group_management import async_recover_group

        pending = self.config_entry.options.get("pending_group_operation")
        if not pending:
            return self.async_abort(reason="no_pending_operation")
        errors, reason = {}, ""
        if user_input is not None:
            if user_input.get("confirmed") is not True:
                errors["base"] = "confirmation_required"
            else:
                try:
                    await async_recover_group(
                        self.hass, self.config_entry, confirmed=True
                    )
                except (
                    HomeAssistantError,
                    RuntimeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    errors["base"] = "group_operation_failed"
                    reason = str(exc)
                else:
                    return self.async_create_entry(
                        title="", data=dict(self.config_entry.options)
                    )
        return self.async_show_form(
            step_id="recover_group",
            data_schema=vol.Schema({vol.Required("confirmed", default=False): bool}),
            errors=errors,
            description_placeholders={
                "group": pending.get("name", pending["id"]),
                "reason": reason,
            },
        )

    async def async_step_control(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self.config_entry.runtime_data.set_mode(
                    ControlMode.ACTIVE if user_input["active"] else ControlMode.OBSERVE,
                    manual=True,
                )
            except (RuntimeError, ValueError, AttributeError) as exc:
                errors["base"] = control_error(exc)
            else:
                return self.async_create_entry(
                    data={**self.config_entry.options, "active": user_input["active"]}
                )
        return self.async_show_form(
            step_id="control",
            data_schema=vol.Schema({vol.Required("active", default=False): bool}),
            errors=errors,
        )

    async def async_step_edit_circuit(self, user_input=None):
        if self.config_entry.options.get("pending_group_operation"):
            return self.async_abort(reason="group_operation_pending")
        choices = {
            item["id"]: _stored_label(item)
            for item in self.config_entry.data.get("lights", [])
        }
        if not choices:
            return self.async_abort(reason="no_circuits")
        if user_input is not None and user_input.get("circuit") in choices:
            self._edit_circuit_id = user_input["circuit"]
            return await self.async_step_edit_circuit_details()
        return self.async_show_form(
            step_id="edit_circuit",
            data_schema=vol.Schema({vol.Required("circuit"): vol.In(choices)}),
        )

    async def async_step_edit_group(self, user_input=None):
        choices = {
            item["id"]: item.get("name", item["id"])
            for item in self.config_entry.data.get("groups", [])
        }
        if not choices:
            return self.async_abort(reason="no_groups")
        if user_input is not None and user_input.get("group") in choices:
            self._edit_group_id = user_input["group"]
            return await self.async_step_edit_group_details()
        return self.async_show_form(
            step_id="edit_group",
            data_schema=vol.Schema({vol.Required("group"): vol.In(choices)}),
        )

    async def async_step_edit_group_details(self, user_input=None):
        from homeassistant.exceptions import HomeAssistantError

        from .group_management import async_edit_group

        data = dict(self.config_entry.data)
        group = next(
            (
                item
                for item in data.get("groups", [])
                if item["id"] == self._edit_group_id
            ),
            None,
        )
        if group is None:
            return self.async_abort(reason="group_missing")
        blocked = {
            member
            for item in data.get("groups", [])
            if item["id"] != group["id"]
            for member in item["members"]
        }
        choices = {
            item["id"]: _stored_label(item)
            for item in data.get("lights", [])
            if item["id"] not in blocked
        }
        pending = self.config_entry.options.get("pending_group_operation", {})
        retrying = pending.get("action") == "edit" and pending.get("id") == group["id"]
        suggested_name = (
            normalize_group_name(pending["new_name"])
            if retrying
            else group.get("name", group["id"])
        )
        suggested_members = pending["members"] if retrying else group["members"]
        errors = {}
        if user_input is not None:
            try:
                name = _group_name(user_input.get("name"))
                members = user_input.get("members")
                if (
                    not isinstance(members, list)
                    or not members
                    or len(set(members)) != len(members)
                    or not set(members) <= set(choices)
                ):
                    raise _FormError("need_two_members")
                if any(
                    item["id"] != group["id"]
                    and z2m_group_name(item.get("name", item["id"]))
                    == z2m_group_name(name)
                    for item in data.get("groups", [])
                ):
                    raise _FormError("already_mapped")
                await async_edit_group(
                    self.hass, self.config_entry, group, name, members
                )
            except _FormError as exc:
                errors["base"] = exc.reason
            except HomeAssistantError, RuntimeError, ValueError, KeyError, TypeError:
                errors["base"] = "group_operation_failed"
            else:
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )
        return self.async_show_form(
            step_id="edit_group_details",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=suggested_name): str,
                    vol.Required("members", default=suggested_members): cv.multi_select(
                        choices
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_edit_circuit_details(self, user_input=None):
        if self.config_entry.options.get("pending_group_operation"):
            return self.async_abort(reason="group_operation_pending")
        errors = {}
        original_data = dict(self.config_entry.data)
        current = copy.deepcopy(original_data)
        original = next(
            (item for item in current["lights"] if item["id"] == self._edit_circuit_id),
            None,
        )
        if original is None:
            return self.async_abort(reason="circuit_missing")
        channel = next(
            item
            for item in current["relay_channels"]
            if item["id"] == original["power_channel"]
        )
        try:
            catalog = await discover_topology(self.hass, current["base_topic"])
        except TimeoutError, ValueError, TypeError, KeyError:
            return self.async_abort(reason="catalog_unavailable")
        other = copy.deepcopy(current)
        other["lights"] = [
            item for item in other["lights"] if item["id"] != original["id"]
        ]
        other["relay_channels"] = [
            item for item in other["relay_channels"] if item["id"] != channel["id"]
        ]
        lights, relays = _catalog_choices(catalog, other)
        if user_input is not None:
            try:
                name = _require_name(user_input.get("name"))
                light = _split_light(user_input["light"])
                relay = _split_relay(user_input["relay"])
                if (
                    user_input["light"] not in lights
                    or user_input["relay"] not in relays
                    or _pair_taken(other, name, light, relay)
                ):
                    raise _FormError("already_mapped")
                group = next(
                    (
                        item
                        for item in current.get("groups", [])
                        if original["id"] in item["members"]
                    ),
                    None,
                )
                remapped_led = light != (
                    original["device_ieee"].lower(),
                    original["endpoint"],
                )
                minimum, maximum = (
                    (original["min_kelvin"], original["max_kelvin"])
                    if light == (original["device_ieee"].lower(), original["endpoint"])
                    else _kelvin_range(catalog, *light)
                )
                original.update(
                    name=name,
                    device_ieee=light[0],
                    endpoint=light[1],
                    min_kelvin=minimum,
                    max_kelvin=maximum,
                )
                channel.update(
                    device_ieee=relay[0], endpoint=relay[1], state_key=relay[2]
                )
                validate_topology(
                    validate_config(current), catalog, require_groups=False
                )
            except _FormError as exc:
                errors["base"] = exc.reason
            except KeyError, TypeError, ValueError, IndexError:
                errors["base"] = "invalid_mapping"
            else:
                if (
                    self.config_entry.options.get("pending_group_operation")
                    or dict(self.config_entry.data) != original_data
                ):
                    return self.async_abort(reason="configuration_changed")
                if group is not None and remapped_led:
                    from homeassistant.exceptions import HomeAssistantError

                    from .group_management import async_edit_group

                    try:
                        await async_edit_group(
                            self.hass,
                            self.config_entry,
                            group,
                            group.get("name", group["id"]),
                            group["members"],
                            mapping={"light": original, "channel": channel},
                        )
                    except (
                        HomeAssistantError,
                        RuntimeError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ):
                        return self.async_show_form(
                            step_id="edit_circuit_details",
                            data_schema=vol.Schema(
                                {
                                    vol.Required(
                                        "name", default=user_input["name"]
                                    ): str,
                                    vol.Required(
                                        "light", default=user_input["light"]
                                    ): vol.In(lights),
                                    vol.Required(
                                        "relay", default=user_input["relay"]
                                    ): vol.In(relays),
                                }
                            ),
                            errors={"base": "group_operation_failed"},
                        )
                    return self.async_create_entry(
                        title="", data=dict(self.config_entry.options)
                    )
                runtime = getattr(self.config_entry, "runtime_data", None)
                if runtime is not None:
                    runtime.set_mode(ControlMode.OBSERVE)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=current
                )
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )
        schema = vol.Schema(
            {
                vol.Required("name", default=_stored_label(original)): str,
                vol.Required(
                    "light", default=f"{original['device_ieee']}|{original['endpoint']}"
                ): vol.In(lights),
                vol.Required(
                    "relay",
                    default=f"{channel['device_ieee']}|{channel['endpoint']}|{channel['state_key']}",
                ): vol.In(relays),
            }
        )
        return self.async_show_form(
            step_id="edit_circuit_details", data_schema=schema, errors=errors
        )

    async def async_step_add_circuit(self, user_input=None):
        if self.config_entry.options.get("pending_group_operation"):
            return self.async_abort(reason="group_operation_pending")
        errors = {}
        if not hasattr(self, "_catalog"):
            try:
                self._catalog = await discover_topology(
                    self.hass, self.config_entry.data["base_topic"]
                )
            except TimeoutError, ValueError, TypeError, KeyError:
                return self.async_abort(reason="catalog_unavailable")
        if self.config_entry.options.get("pending_group_operation"):
            return self.async_abort(reason="group_operation_pending")
        current = dict(self.config_entry.data)
        light_options, relay_options = _catalog_choices(self._catalog, current)
        if user_input is None and (not light_options or not relay_options):
            return self.async_abort(reason="no_free_circuit")
        if user_input is not None:
            try:
                channel, configured_light = _circuit_records(
                    _circuit_fields(
                        self._catalog,
                        user_input,
                        current,
                        self.config_entry.options.get("retired_ids", ()),
                    )
                )
                data = copy.deepcopy(current)
                data["relay_channels"].append(channel)
                data["lights"].append(configured_light)
                config = validate_config(data)
                validate_topology(config, self._catalog, require_groups=False)
            except _FormError as exc:
                errors["base"] = exc.reason
            except TypeError, ValueError, KeyError, IndexError:
                errors["base"] = "invalid_mapping"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )
        schema = {
            vol.Required("name"): str,
            vol.Required("light"): vol.In(light_options),
            vol.Required("relay"): vol.In(relay_options),
        }
        return self.async_show_form(
            step_id="add_circuit",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_create_group(self, user_input=None):
        if self.config_entry.options.get("pending_group_operation"):
            return self.async_abort(reason="group_operation_pending")
        errors = {}
        current = dict(self.config_entry.data)
        free = _free_lights(current)
        if not free:
            return self.async_abort(reason="need_two_circuits")
        options = {
            str(light["id"]): circuit_label(str(light["id"]), light.get("name"))
            for light in free
        }
        if user_input is not None:
            try:
                name = _group_name(user_input.get("name"))
                if z2m_group_name(name) in _known_group_names(current):
                    raise _FormError("already_mapped")
                catalog = await discover_topology(self.hass, current["base_topic"])
                if catalog.group_named(z2m_group_name(name)) is not None:
                    raise _FormError("already_mapped")
                chosen = user_input.get("members")
                if not isinstance(chosen, list) or not set(chosen) <= set(options):
                    raise _FormError("invalid_mapping")
                order = [str(light["id"]) for light in free]
                members = [item for item in order if item in set(chosen)]
                if not members:
                    raise _FormError("need_two_members")
                data = copy.deepcopy(current)
                data.setdefault("groups", []).append(
                    {
                        "id": _next_group_id(
                            data, self.config_entry.options.get("retired_ids", ())
                        ),
                        "name": name,
                        "members": members,
                    }
                )
                validate_config(data)
            except _FormError as exc:
                errors["base"] = exc.reason
            except TimeoutError:
                errors["base"] = "catalog_unavailable"
            except TypeError, ValueError, KeyError, IndexError:
                errors["base"] = "invalid_mapping"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )
        return self.async_show_form(
            step_id="create_group",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("members"): cv.multi_select(options),
                }
            ),
            errors=errors,
        )
