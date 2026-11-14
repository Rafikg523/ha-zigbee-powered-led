"""Synthetic capabilities from Zigbee2MQTT exposes."""

import pytest

from custom_components.zigbee_led_orchestrator.light_capabilities import (
    LightCapabilities,
)


def test_brightness_scales_to_documented_device_range():
    capabilities = LightCapabilities.from_exposes(
        [
            {
                "type": "light",
                "features": [{"name": "brightness", "value_min": 1, "value_max": 100}],
            }
        ]
    )
    assert capabilities.device_brightness(1) == 1
    assert capabilities.device_brightness(255) == 100
    assert capabilities.device_brightness(128) == 50


def test_missing_brightness_capability_is_not_guessed():
    capabilities = LightCapabilities.from_exposes([])
    with pytest.raises(ValueError):
        capabilities.device_brightness(100)


def test_color_temp_clamps_to_documented_mired_range():
    capabilities = LightCapabilities.from_exposes(
        [
            {
                "type": "light",
                "features": [
                    {"name": "color_temp", "value_min": 200, "value_max": 400}
                ],
            }
        ]
    )
    assert capabilities.device_mired(10000) == 200
    assert capabilities.device_mired(1000) == 400


def test_boolean_range_does_not_count_as_number():
    with pytest.raises(ValueError):
        LightCapabilities.from_exposes(
            [
                {
                    "type": "light",
                    "features": [
                        {"name": "brightness", "value_min": True, "value_max": 100}
                    ],
                }
            ]
        )


def test_multi_endpoint_exposes_only_use_requested_endpoint():
    exposes = [
        {
            "type": "light",
            "endpoint": "left",
            "features": [{"name": "brightness", "value_min": 1, "value_max": 100}],
        },
        {
            "type": "light",
            "endpoint": "right",
            "features": [{"name": "brightness", "value_min": 1, "value_max": 200}],
        },
    ]
    capabilities = LightCapabilities.from_exposes(exposes, endpoint="right")
    assert capabilities.device_brightness(255) == 200
    assert (
        LightCapabilities.from_exposes(exposes, endpoint="missing").brightness_range
        is None
    )


def test_reported_brightness_is_normalized_to_ha_range():
    capabilities = LightCapabilities((1, 100), (200, 400))
    assert capabilities.ha_brightness(100) == 255
    assert capabilities.ha_brightness(1) == 1
    assert capabilities.ha_brightness(50) == 127
    with pytest.raises(ValueError):
        capabilities.ha_brightness(101)


def test_zero_minimum_brightness_is_supported_without_commanding_off():
    capabilities = LightCapabilities.from_exposes(
        [
            {
                "type": "light",
                "features": [{"name": "brightness", "value_min": 0, "value_max": 254}],
            }
        ]
    )
    assert capabilities.brightness_range == (1, 254)
    assert capabilities.device_brightness(1) == 1
    assert capabilities.ha_brightness(254) == 255
