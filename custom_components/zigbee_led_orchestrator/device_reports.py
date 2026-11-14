"""Decode session-fenced attribute reports from the optional Z2M extension.

Only raw genOnOff/genLevelCtrl/lightingColorCtrl report/readResponse attributes
are supported. Merged Z2M state and optimistic updates never enter this path.
"""

from __future__ import annotations

import secrets

from .models import (
    EvidenceQuality,
    LedObservation,
    OrchestratorConfig,
    RelayObservation,
    SwitchState,
)


class DeviceReports:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.session = secrets.token_hex(16)
        self.sequence = 0
        self.ready = False
        self.broken = False
        self._relays: dict[str, SwitchState] = {}
        self._values: dict[str, dict] = {}

    def invalidate_leds(self, light_ids: tuple[str, ...]) -> None:
        """A publication requires new attribute proof after its completion."""
        for light_id in light_ids:
            self._values.pop(light_id, None)

    def request(self) -> dict:
        return {
            "session": self.session,
            "targets": [
                {"ieee": ieee, "endpoint": endpoint}
                for ieee, endpoint in sorted(
                    {
                        (item.device_ieee, item.endpoint)
                        for item in (*self.config.relay_channels, *self.config.lights)
                    }
                )
            ],
        }

    def ingest(self, payload: object, *, retained: bool, now: float) -> tuple:
        if (
            self.broken
            or retained
            or not isinstance(payload, dict)
            or payload.get("session") != self.session
        ):
            return ()
        if payload.get("type") == "ready" and payload.get("sequence") == 0:
            self.ready = True
            return ()
        sequence = payload.get("sequence")
        if (
            type(sequence) is not int
            or sequence <= self.sequence
            or payload.get("type") not in ("attributeReport", "readResponse")
            or type(payload.get("endpoint")) is not int
            or not isinstance(payload.get("data"), dict)
        ):
            return ()
        if sequence != self.sequence + 1:
            # A missing OFF may conceal an entire power epoch. Never bridge it.
            self.broken = True
            self.ready = False
            self._relays.clear()
            self._values.clear()
            return ()
        address = (payload.get("ieee"), payload["endpoint"])
        channels = [
            c
            for c in self.config.relay_channels
            if (c.device_ieee, c.endpoint) == address
        ]
        lights = [
            c for c in self.config.lights if (c.device_ieee, c.endpoint) == address
        ]
        if not channels and not lights:
            return ()
        data, cluster = payload["data"], payload.get("cluster")
        attributes = {}
        if (
            cluster == "genOnOff"
            and type(data.get("onOff")) is int
            and data["onOff"] in (0, 1)
        ):
            attributes["state"] = SwitchState.ON if data["onOff"] else SwitchState.OFF
        elif (
            cluster == "genLevelCtrl"
            and type(data.get("currentLevel")) is int
            and 0 <= data["currentLevel"] <= 254
        ):
            attributes["brightness"] = data["currentLevel"]
        elif (
            cluster == "lightingColorCtrl"
            and type(data.get("colorTemperature")) is int
            and data["colorTemperature"] > 0
        ):
            attributes["color_temp_kelvin"] = round(
                1_000_000 / data["colorTemperature"]
            )
        if not attributes:
            return ()
        self.sequence = sequence
        result = []
        if len(channels) == 1 and "state" in attributes:
            channel = channels[0]
            state = attributes["state"]
            if state != SwitchState.ON or self._relays.get(channel.id) != state:
                for light in self.config.lights:
                    if light.power_channel == channel.id:
                        self._values.pop(light.id, None)
            self._relays[channel.id] = state
            result.append(
                RelayObservation(
                    channel.id, state, now, False, EvidenceQuality.DEVICE_REPORT
                )
            )
        for light in lights:
            if self._relays.get(light.power_channel) != SwitchState.ON:
                continue
            kelvin = attributes.get("color_temp_kelvin")
            if (
                kelvin is not None
                and not light.min_kelvin <= kelvin <= light.max_kelvin
            ):
                continue
            values = self._values.setdefault(light.id, {})
            values.update(attributes)
            result.append(
                LedObservation(
                    light.id,
                    values.get("state", SwitchState.UNKNOWN),
                    now,
                    False,
                    EvidenceQuality.DEVICE_REPORT,
                    values.get("brightness"),
                    values.get("color_temp_kelvin"),
                )
            )
        return tuple(result)
