"""Configuration safety checks without Home Assistant or real devices."""

import copy
import unittest


def valid_config():
    """Return synthetic configuration, never real installation data."""
    return {
        "base_topic": "zigbee2mqtt",
        "relay_channels": [
            {
                "id": "relay_a",
                "device_ieee": "0x0000000000000001",
                "endpoint": 1,
                "state_key": "state_l1",
            },
            {
                "id": "relay_b",
                "device_ieee": "0x0000000000000001",
                "endpoint": 2,
                "state_key": "state_l2",
            },
        ],
        "lights": [
            {
                "id": "light_a",
                "device_ieee": "0x0000000000000002",
                "endpoint": 1,
                "power_channel": "relay_a",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            },
            {
                "id": "light_b",
                "device_ieee": "0x0000000000000003",
                "endpoint": 1,
                "power_channel": "relay_b",
                "min_kelvin": 2000,
                "max_kelvin": 6500,
            },
        ],
        "groups": [{"id": "group_a", "z2m_id": 1, "members": ["light_a", "light_b"]}],
        "rooms": [{"id": "room_a", "groups": ["group_a"]}],
        "policies": {"min_send_interval_ms": 100, "publication_timeout_ms": 3000},
    }


class TestConfiguration(unittest.TestCase):
    def validate(self, config):
        from custom_components.zigbee_led_orchestrator.configuration import (
            validate_config,
        )

        return validate_config(config)

    def test_valid_mapping_has_stable_identifiers(self):
        result = self.validate(valid_config())
        self.assertEqual(result.lights[0].device_ieee, "0x0000000000000002")
        self.assertEqual(result.groups[0].members, ("light_a", "light_b"))
        self.assertFalse(result.policies.startup_restore_power)
        self.assertFalse(result.policies.z2m_set_retain)

    def test_duplicate_channel_rejected(self):
        config = valid_config()
        config["relay_channels"][1]["endpoint"] = 1
        config["relay_channels"][1]["state_key"] = "state_l1"
        with self.assertRaisesRegex(ValueError, "relay_channels"):
            self.validate(config)

    def test_duplicate_state_key_rejected(self):
        config = valid_config()
        config["relay_channels"][1]["state_key"] = "state_l1"
        with self.assertRaisesRegex(ValueError, "relay_channels"):
            self.validate(config)

    def test_multiple_lights_cannot_share_dedicated_channel(self):
        config = valid_config()
        config["lights"][1]["power_channel"] = "relay_a"
        with self.assertRaisesRegex(ValueError, "power_channel"):
            self.validate(config)

    def test_missing_member_rejected(self):
        config = valid_config()
        config["groups"][0]["members"].append("missing")
        with self.assertRaisesRegex(ValueError, "members"):
            self.validate(config)

    def test_unknown_power_channel_rejected(self):
        config = valid_config()
        config["lights"][0]["power_channel"] = "missing"
        with self.assertRaisesRegex(ValueError, "power_channel"):
            self.validate(config)

    def test_invalid_numeric_policy_rejected(self):
        for value in (True, float("nan"), -1, 0, "100"):
            with self.subTest(value=value):
                config = valid_config()
                config["policies"]["min_send_interval_ms"] = value
                with self.assertRaisesRegex(ValueError, "min_send_interval_ms"):
                    self.validate(config)

    def test_safety_flags_cannot_enable_replay_or_retain(self):
        for field in ("startup_restore_power", "z2m_set_retain"):
            with self.subTest(field=field):
                config = valid_config()
                config["policies"][field] = True
                with self.assertRaisesRegex(ValueError, field):
                    self.validate(config)

    def test_restore_parameters_requires_boolean(self):
        config = valid_config()
        config["policies"]["restore_parameters"] = "true"
        with self.assertRaisesRegex(ValueError, "restore_parameters"):
            self.validate(config)
        config["policies"]["restore_parameters"] = True
        self.assertTrue(self.validate(config).policies.restore_parameters)

    def test_group_overlap_rejected_for_mvp(self):
        config = valid_config()
        config["groups"].append({"id": "group_b", "z2m_id": 2, "members": ["light_a"]})
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.validate(config)

    def test_invalid_temperature_limits_rejected(self):
        config = valid_config()
        config["lights"][0]["min_kelvin"] = 7000
        with self.assertRaisesRegex(ValueError, "kelvin"):
            self.validate(config)

    def test_unknown_fields_are_not_silently_discarded(self):
        config = valid_config()
        config["policies"]["startup_restore_pwer"] = False
        with self.assertRaisesRegex(ValueError, "startup_restore_pwer"):
            self.validate(config)

    def test_input_is_not_modified(self):
        config = valid_config()
        original = copy.deepcopy(config)
        self.validate(config)
        self.assertEqual(config, original)

    def test_same_device_endpoint_can_expose_two_channels(self):
        config = valid_config()
        config["relay_channels"][1]["endpoint"] = 1
        result = self.validate(config)
        self.assertEqual(len(result.relay_channels), 2)

    def test_zero_slider_debounce_can_disable_coalescing(self):
        config = valid_config()
        config["policies"]["slider_coalesce_ms"] = 0
        result = self.validate(config)
        self.assertEqual(result.policies.slider_coalesce_ms, 0)

    def test_empty_install_has_no_circuits_or_groups(self):
        config = valid_config()
        config["relay_channels"] = []
        config["lights"] = []
        config["groups"] = []
        config["rooms"] = []
        result = self.validate(config)
        self.assertEqual(result.lights, ())
        self.assertEqual(result.groups, ())

    def test_missing_name_uses_identifier_label(self):
        result = self.validate(valid_config())
        self.assertEqual(result.lights[0].name, "Light A")
        self.assertEqual(result.lights[1].name, "Light B")

    def test_explicit_name_is_kept(self):
        config = valid_config()
        config["lights"][0]["name"] = "Kuchnia"
        self.assertEqual(self.validate(config).lights[0].name, "Kuchnia")

    def test_blank_name_rejected(self):
        for value in ("", "  ", " Kuchnia"):
            with self.subTest(value=value):
                config = valid_config()
                config["lights"][0]["name"] = value
                with self.assertRaisesRegex(ValueError, "name"):
                    self.validate(config)

    def test_duplicate_name_rejected(self):
        config = valid_config()
        config["lights"][0]["name"] = "Kuchnia"
        config["lights"][1]["name"] = "Kuchnia"
        with self.assertRaisesRegex(ValueError, "lights.name"):
            self.validate(config)
