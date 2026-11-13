"""Tests for domain value conversions and command defaults."""

import unittest


class TestModels(unittest.TestCase):
    def test_kelvin_to_mired(self):
        from custom_components.zigbee_led_orchestrator.configuration import (
            kelvin_to_mired,
        )

        self.assertEqual(kelvin_to_mired(4000), 250)
        self.assertEqual(kelvin_to_mired(2000), 500)
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                kelvin_to_mired(invalid)

    def test_snapshot_distinguishes_request_and_confirmation(self):
        from custom_components.zigbee_led_orchestrator.models import (
            DesiredState,
            EffectiveState,
            LightSnapshot,
            Phase,
        )

        desired = DesiredState(
            on=True,
            brightness=180,
            color_temp_kelvin=4000,
            generation=1,
            source="test",
            updated_at=1.0,
        )
        snapshot = LightSnapshot(
            desired=desired,
            phase=Phase.POWERING,
            effective=EffectiveState.UNKNOWN,
            confirmed=False,
            available=True,
            reason=None,
        )
        self.assertTrue(snapshot.desired.on)
        self.assertFalse(snapshot.confirmed)
        self.assertEqual(snapshot.effective, EffectiveState.UNKNOWN)

    def test_brightness_zero_turns_off_without_erasing_preference(self):
        from custom_components.zigbee_led_orchestrator.models import IntentPatch

        patch = IntentPatch(on=True, brightness=0)
        self.assertFalse(patch.on)
        self.assertIsNone(patch.brightness)

    def test_patch_rejects_invalid_brightness_and_temperature(self):
        from custom_components.zigbee_led_orchestrator.models import IntentPatch

        for kwargs in (
            {"brightness": True},
            {"brightness": 256},
            {"color_temp_kelvin": 0},
            {"color_temp_kelvin": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                IntentPatch(**kwargs)

    def test_observation_retains_report_parameters_and_retained_flags(self):
        from custom_components.zigbee_led_orchestrator.models import (
            EvidenceQuality,
            LightObservation,
            SwitchState,
        )

        observation = LightObservation(
            power_epoch=2,
            bridge_epoch=1,
            relay_state=SwitchState.ON,
            led_state=SwitchState.ON,
            relay_quality=EvidenceQuality.DEVICE_REPORT,
            led_quality=EvidenceQuality.UNKNOWN_SOURCE,
            relay_retained=False,
            led_retained=True,
            brightness=180,
            color_temp_kelvin=4000,
        )
        self.assertFalse(observation.relay_retained)
        self.assertTrue(observation.led_retained)
        self.assertEqual(observation.brightness, 180)
        self.assertEqual(observation.color_temp_kelvin, 4000)

    def test_send_request_copies_nested_payload_before_queuing(self):
        from custom_components.zigbee_led_orchestrator.models import (
            CommandKind,
            CommandPriority,
            SendRequest,
        )

        payload = {"state": "ON", "nested": {"values": [1, {"color": 250}]}}
        request = SendRequest(
            topic="z2m/light/set",
            payload=payload,
            tokens=(),
            resources=("light",),
            priority=CommandPriority.NORMAL,
            operation_id="op-1",
            kind=CommandKind.LED_SET,
            topology_version=1,
        )
        payload["state"] = "OFF"
        payload["nested"]["values"][1]["color"] = 500
        first_read = request.payload
        first_read["nested"]["values"].append("changed")
        self.assertEqual(
            request.payload, {"state": "ON", "nested": {"values": [1, {"color": 250}]}}
        )

    def test_send_request_rejects_non_json_values(self):
        from custom_components.zigbee_led_orchestrator.models import (
            CommandKind,
            CommandPriority,
            SendRequest,
        )

        for payload in ({"value": float("nan")}, {"value": {1, 2}}, {1: "bad-key"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                SendRequest(
                    topic="z2m/light/set",
                    payload=payload,
                    tokens=(),
                    resources=(),
                    priority=CommandPriority.NORMAL,
                    operation_id="op-1",
                    kind=CommandKind.LED_SET,
                    topology_version=1,
                )
