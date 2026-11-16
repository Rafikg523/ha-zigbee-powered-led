"""Exercise two transport stages, including OFF behind an already accepted SET."""

from custom_components.zigbee_led_orchestrator.desired_store import DesiredStateStore
from custom_components.zigbee_led_orchestrator.models import (
    EvidenceQuality,
    IntentPatch,
    LedObservation,
    PolicyConfig,
    RelayObservation,
    SwitchState,
)
from custom_components.zigbee_led_orchestrator.observation_store import ObservationStore
from custom_components.zigbee_led_orchestrator.reconciler import Circuit, Reconciler
from tests.helpers.clock import FakeClock
from tests.helpers.delayed_transport import DelayedDeviceTransport


async def test_delayed_device_executes_first_then_latest_and_off_stays_bounded():
    clock = FakeClock(start=1)
    observations = ObservationStore({"led": "relay"})
    physical = {"brightness": 100, "kelvin": 3000, "state": SwitchState.ON}

    def report_led(*, read=False):
        engine.notify(
            observations.ingest(
                LedObservation(
                    "led",
                    physical["state"],
                    clock.now(),
                    False,
                    EvidenceQuality.DEVICE_REPORT,
                    physical["brightness"],
                    physical["kelvin"],
                    read_response_times=(
                        ("state", clock.now()),
                        ("brightness", clock.now()),
                        ("color_temp", clock.now()),
                    )
                    if read
                    else (),
                )
            )
        )

    def execute(publication):
        if "relay" in publication.topic:
            engine.notify(
                observations.ingest(
                    RelayObservation(
                        "relay",
                        SwitchState(publication.payload["state_l1"]),
                        clock.now(),
                        False,
                        EvidenceQuality.DEVICE_REPORT,
                    )
                )
            )
            return
        if publication.topic.endswith("/set"):
            for key in ("brightness", "state"):
                if key in publication.payload:
                    physical[key] = (
                        SwitchState(publication.payload[key])
                        if key == "state"
                        else publication.payload[key]
                    )
        report_led(read=publication.topic.endswith("/get"))

    transport = DelayedDeviceTransport(
        clock, route=lambda topic: topic.split("/")[1], execute=execute
    )
    engine = Reconciler(
        circuits=(
            Circuit(
                "led",
                "relay",
                "example/relay/set",
                "example/led/set",
                "relay",
                "led",
                "state_l1",
            ),
        ),
        desired=DesiredStateStore(("led",), clock=clock),
        observations=observations,
        transport=transport,
        clock=clock,
        policy=PolicyConfig(min_send_interval_ms=1, publication_timeout_ms=500),
        enabled=True,
        probe_enabled=True,
    )
    try:
        engine.notify(
            observations.ingest(
                RelayObservation(
                    "relay",
                    SwitchState.ON,
                    clock.now(),
                    False,
                    EvidenceQuality.DEVICE_REPORT,
                )
            )
        )
        await clock.advance(0.01)
        report_led()
        engine.submit(
            ("led",),
            IntentPatch(on=True, brightness=100, color_temp_kelvin=3000),
            source="ui",
        )
        await clock.advance(2.6)
        for value in (255, 56, 191, 56):
            engine.submit(("led",), IntentPatch(brightness=value), source="slider")
            await clock.advance(0.3)
        assert [
            p.payload.get("brightness")
            for p in transport.sent
            if p.topic.endswith("/set")
        ] == [255]
        await clock.advance(10)
        assert [
            p.payload["brightness"]
            for p in transport.executed
            if "brightness" in p.payload and p.topic.endswith("/set")
        ] == [255, 56]
        assert physical["brightness"] == 56
        assert engine.snapshot("led").confirmed
        engine.submit(("led",), IntentPatch(brightness=190), source="slider")
        await clock.advance(0.3)
        started = clock.now()
        engine.submit(("led",), IntentPatch(on=False), source="ui")
        await clock.advance(2.1)
        assert (
            next(p for p in transport.sent if p.payload == {"state_l1": "OFF"}).sent_at
            - started
            <= 2.01
        )
        await clock.advance(15)
        assert engine.snapshot("led").confirmed
        assert physical["state"] == SwitchState.OFF
        assert (
            transport.maximum_depth["led"] <= 4
        )  # one SET, one read, priority OFF + read
    finally:
        await engine.close()
        await transport.close()
