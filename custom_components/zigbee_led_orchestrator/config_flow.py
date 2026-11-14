"""Safe configuration flow based on an observed Zigbee2MQTT catalog."""

from __future__ import annotations

import asyncio
import json

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback

from .configuration import validate_config
from .const import DOMAIN
from .light_capabilities import LightCapabilities
from .models import ControlMode
from .topology import Topology, validate_topology


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
                    self._catalog = await discover_topology(self.hass, base_topic)
                except TimeoutError, ValueError, TypeError, KeyError:
                    errors["base"] = "catalog_unavailable"
                else:
                    self._report_extension = user_input.get("report_extension", False)
                    self._base_topic = base_topic
                    return await self.async_step_mapping()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("base_topic", default="zigbee2mqtt"): str,
                    vol.Optional("report_extension", default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_mapping(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                if "configuration" in user_input or "lights" in user_input:
                    data = dict(user_input.get("configuration", user_input))
                    data["base_topic"] = self._base_topic
                else:
                    lights = [user_input[f"light_{i}"].split("|") for i in (1, 2)]
                    relays = [user_input[f"relay_{i}"].split("|") for i in (1, 2)]
                    ranges = []
                    for ieee, endpoint in lights:
                        device = self._catalog.resolve_device(ieee)
                        capability = LightCapabilities.from_exposes(
                            device.capabilities,
                            endpoint=device.endpoint_name(int(endpoint)),
                        )
                        mired = capability.mired_range
                        ranges.append(
                            (round(1_000_000 / mired[1]), round(1_000_000 / mired[0]))
                            if mired
                            else (2000, 6500)
                        )
                    data = {
                        "base_topic": self._base_topic,
                        "relay_channels": [
                            {
                                "id": f"relay_{i}",
                                "device_ieee": values[0],
                                "endpoint": int(values[1]),
                                "state_key": values[2],
                            }
                            for i, values in enumerate(relays, 1)
                        ],
                        "lights": [
                            {
                                "id": f"light_{i}",
                                "device_ieee": values[0],
                                "endpoint": int(values[1]),
                                "power_channel": f"relay_{i}",
                                "min_kelvin": user_input.get(
                                    f"min_kelvin_{i}", ranges[i - 1][0]
                                ),
                                "max_kelvin": user_input.get(
                                    f"max_kelvin_{i}", ranges[i - 1][1]
                                ),
                            }
                            for i, values in enumerate(lights, 1)
                        ],
                        "groups": (
                            [
                                {
                                    "id": "group_1",
                                    "z2m_id": int(user_input["group"]),
                                    "members": ["light_1", "light_2"],
                                }
                            ]
                            if user_input.get("group")
                            else []
                        ),
                        "rooms": [],
                        "policies": {
                            "min_send_interval_ms": user_input["min_send_interval_ms"],
                            "publication_timeout_ms": user_input[
                                "publication_timeout_ms"
                            ],
                        },
                    }
                    for key, default in (
                        ("boot_grace_ms", 2500),
                        ("readiness_timeout_ms", 8000),
                    ):
                        data["policies"][key] = user_input.get(key, default)
                data["report_extension"] = self._report_extension
                config = validate_config(data)
                if (
                    len(config.lights) != 2
                    or len(config.relay_channels) != 2
                    or len(config.groups) > 1
                ):
                    raise ValueError(
                        "Pilot requires two circuits and at most one group"
                    )
                validate_topology(config, self._catalog)
            except TypeError, ValueError, KeyError, IndexError:
                errors["base"] = "invalid_mapping"
            else:
                if self._async_current_entries():
                    return self.async_abort(reason="single_instance")
                return self.async_create_entry(
                    title="Zigbee LED Orchestrator", data=data
                )
        light_options = {}
        relay_options = {}
        for device in self._catalog.devices():
            for endpoint in device.endpoints:
                label = f"{device.topic} — {device.ieee} / {endpoint}"
                endpoint_name = device.endpoint_name(endpoint)
                if any(
                    isinstance(expose, dict)
                    and expose.get("type") == "light"
                    and expose.get("endpoint") == endpoint_name
                    for expose in device.capabilities
                ):
                    light_options[f"{device.ieee}|{endpoint}"] = label
                for expose in device.capabilities:
                    if (
                        not isinstance(expose, dict)
                        or expose.get("type") != "switch"
                        or expose.get("endpoint") != endpoint_name
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
                            and feature.get("value_on") == "ON"
                            and feature.get("value_off") == "OFF"
                            and type(feature.get("access")) is int
                            and feature["access"] & 2
                        ):
                            relay_options[f"{device.ieee}|{endpoint}|{key}"] = (
                                f"{label} / {key}"
                            )
        light_addresses = {
            (key.split("|")[0], int(key.split("|")[1])) for key in light_options
        }
        group_options = {"": "—"}
        group_options.update(
            {
                str(group_id): group.topic
                for group_id, group in self._catalog.groups()
                if len(group.members) == 2 and group.members <= light_addresses
            }
        )
        schema = {vol.Required(f"light_{i}"): vol.In(light_options) for i in (1, 2)}
        schema.update(
            {vol.Required(f"relay_{i}"): vol.In(relay_options) for i in (1, 2)}
        )
        schema[vol.Optional("group", default="")] = vol.In(group_options)
        schema[vol.Required("min_send_interval_ms", default=100)] = vol.All(
            vol.Coerce(int), vol.Range(min=1)
        )
        schema[vol.Required("publication_timeout_ms", default=3000)] = vol.All(
            vol.Coerce(int), vol.Range(min=1)
        )
        for key, default in (("boot_grace_ms", 2500), ("readiness_timeout_ms", 8000)):
            schema[vol.Required(key, default=default)] = vol.All(
                vol.Coerce(int), vol.Range(min=1)
            )
        return self.async_show_form(
            step_id="mapping", data_schema=vol.Schema(schema), errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Changing mode is explicit and does not restore old intentions."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self.config_entry.runtime_data.set_mode(
                    ControlMode.ACTIVE if user_input["active"] else ControlMode.OBSERVE
                )
            except RuntimeError, ValueError, AttributeError:
                errors["base"] = "control_unavailable"
            else:
                return self.async_create_entry(data={"active": user_input["active"]})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("active", default=False): bool}),
            errors=errors,
        )
