"""Convert HA light parameters using explicit Zigbee2MQTT exposes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LightCapabilities:
    """Only validated device ranges are usable for commands."""

    brightness_range: tuple[int, int] | None
    mired_range: tuple[int, int] | None

    @classmethod
    def from_exposes(
        cls, exposes: list[object], *, endpoint: str | None = None
    ) -> LightCapabilities:
        ranges: dict[str, tuple[int, int]] = {}
        for expose in exposes:
            if not isinstance(expose, dict) or expose.get("type") != "light":
                continue
            expose_endpoint = expose.get("endpoint")
            if expose_endpoint != endpoint:
                continue
            features = expose.get("features", [])
            if not isinstance(features, list):
                raise TypeError("invalid light features")
            for feature in features:
                if not isinstance(feature, dict):
                    raise TypeError("invalid feature")
                name = feature.get("name")
                if name not in ("brightness", "color_temp"):
                    continue
                minimum, maximum = feature.get("value_min"), feature.get("value_max")
                if (
                    type(minimum) is not int
                    or type(maximum) is not int
                    or minimum < (0 if name == "brightness" else 1)
                    or maximum < minimum
                    or name in ranges
                ):
                    raise ValueError("invalid or ambiguous capability range")
                if name == "brightness":
                    minimum = max(1, minimum)
                    if maximum < minimum:
                        raise ValueError("brightness has no nonzero range")
                ranges[name] = (minimum, maximum)
        return cls(ranges.get("brightness"), ranges.get("color_temp"))

    def device_brightness(self, brightness: int) -> int:
        if type(brightness) is not int or not 1 <= brightness <= 255:
            raise ValueError("brightness must be in 1..255")
        if self.brightness_range is None:
            raise ValueError("brightness capability is unknown")
        minimum, maximum = self.brightness_range
        return round(minimum + (brightness - 1) * (maximum - minimum) / 254)

    def device_mired(self, kelvin: int) -> int:
        if type(kelvin) is not int or kelvin <= 0:
            raise ValueError("kelvin must be positive")
        if self.mired_range is None:
            raise ValueError("color temperature capability is unknown")
        minimum, maximum = self.mired_range
        return max(minimum, min(maximum, round(1_000_000 / kelvin)))

    def ha_brightness(self, brightness: int) -> int:
        if self.brightness_range is None:
            raise ValueError("brightness capability is unknown")
        minimum, maximum = self.brightness_range
        if type(brightness) is not int or not minimum <= brightness <= maximum:
            raise ValueError("reported brightness outside capability range")
        if minimum == maximum:
            return 255
        return round(1 + (brightness - minimum) * 254 / (maximum - minimum))
