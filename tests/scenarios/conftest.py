import pytest

from tests.scenarios.harness import Rig, ScenarioClock, trusting_adapter_class


@pytest.fixture
async def rig(
    hass, mqtt_mock, enable_custom_integrations, monkeypatch, hass_storage, request
):
    from custom_components.zigbee_led_orchestrator import mqtt_adapter, runtime

    clock = ScenarioClock(start=1000.0)
    adapters = []
    adapter_class = (
        mqtt_adapter.MqttAdapter
        if getattr(request, "param", False)
        else trusting_adapter_class(mqtt_adapter.MqttAdapter)
    )

    def factory(hass_, config):
        adapter = adapter_class(hass_, config, now=clock.now)
        adapters.append(adapter)
        return adapter

    original = mqtt_adapter.mqtt.async_subscribe_connection_status
    callbacks = []

    def capture(hass_, callback):
        callbacks.append(callback)
        return original(hass_, callback)

    monkeypatch.setattr(mqtt_adapter.mqtt, "async_subscribe_connection_status", capture)
    monkeypatch.setattr(mqtt_adapter, "MqttAdapter", factory)
    monkeypatch.setattr(runtime, "MonotonicClock", lambda: clock)
    instance = Rig(hass, mqtt_mock, clock, adapters)
    instance.connection_callbacks = callbacks
    yield instance
    await instance.unload()
