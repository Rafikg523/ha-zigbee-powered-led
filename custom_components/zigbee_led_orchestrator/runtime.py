"""Coordinate the HA MQTT adapter and core without implicit activation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .desired_store import DesiredStateStore
from .light_capabilities import LightCapabilities
from .models import (
    BridgeObservation,
    ControlMode,
    EffectiveState,
    IntentPatch,
    LightSnapshot,
    Phase,
)
from .observation_store import ObservationStore
from .reconciler import Circuit, Reconciler
from .topology import validate_topology


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class Runtime:
    """Own a single reconciler and revoke its authority on topology changes."""

    def __init__(self, config, adapter) -> None:
        self.config = config
        self.adapter = adapter
        self.mode = ControlMode.OBSERVE
        self._clock = MonotonicClock()
        self._desired = DesiredStateStore(
            (light.id for light in config.lights), clock=self._clock
        )
        self._observations = ObservationStore(
            {light.id: light.power_channel for light in config.lights}
        )
        self._engine: Reconciler | None = None
        self._stale = False
        self._closed = False
        self._subscribers: set[Callable[[frozenset[str]], None]] = set()
        self._remove_topology = None

    async def start(self) -> None:
        self._remove_topology = self.adapter.subscribe_topology(self._topology_changed)
        try:
            await self.adapter.start(self._observation)
        except BaseException:
            await self.close()
            raise

    def _topology_changed(self, version: int) -> None:
        if self._engine is not None:
            self._stale = True
            self._engine.set_enabled(False)
            self.adapter.set_active(False)
            self.mode = ControlMode.OBSERVE
        self._announce(frozenset(light.id for light in self.config.lights))

    def _observation(self, observation) -> None:
        delta = self._observations.ingest(observation)
        if isinstance(observation, BridgeObservation) and not observation.online:
            self.mode = ControlMode.OBSERVE
            self.adapter.set_active(False)
            if self._engine is not None:
                self._engine.set_enabled(False)
        if self._engine is not None:
            self._engine.notify(delta)
        else:
            self._announce(delta.light_ids)

    def _announce(self, ids: frozenset[str]) -> None:
        for callback in tuple(self._subscribers):
            callback(ids)

    def subscribe(
        self, callback: Callable[[frozenset[str]], None]
    ) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def light_capabilities(self, light_id: str) -> LightCapabilities:
        light = next(item for item in self.config.lights if item.id == light_id)
        try:
            device = self.adapter.topology.resolve_device(light.device_ieee)
            return LightCapabilities.from_exposes(
                device.capabilities, endpoint=device.endpoint_name(light.endpoint)
            )
        except KeyError, ValueError, TypeError:
            return LightCapabilities(None, None)

    def _make_engine(self) -> Reconciler:
        validate_topology(self.config, self.adapter.topology)
        channels = {channel.id: channel for channel in self.config.relay_channels}
        circuits = []
        for light in self.config.lights:
            channel = channels[light.power_channel]
            relay = self.adapter.topology.resolve_device(channel.device_ieee)
            led = self.adapter.topology.resolve_device(light.device_ieee)
            circuits.append(
                Circuit(
                    light.id,
                    channel.id,
                    relay.topic + "/set",
                    led.topic + "/set",
                    f"{self.config.base_topic}:{channel.device_ieee}:{channel.endpoint}:{channel.state_key}",
                    f"{self.config.base_topic}:{light.device_ieee}:{light.endpoint}",
                    channel.state_key,
                )
            )
        engine = Reconciler(
            circuits=tuple(circuits),
            desired=self._desired,
            observations=self._observations,
            transport=self.adapter,
            clock=self._clock,
            policy=self.config.policies,
        )
        for light in self.config.lights:
            device = self.adapter.topology.resolve_device(light.device_ieee)
            capabilities = LightCapabilities.from_exposes(
                device.capabilities, endpoint=device.endpoint_name(light.endpoint)
            )
            engine.set_capabilities(light.id, capabilities)
        engine.subscribe(self._announce)
        return engine

    def set_mode(self, mode: ControlMode) -> None:
        if self._closed or not isinstance(mode, ControlMode):
            raise RuntimeError("invalid or closed runtime")
        if mode == ControlMode.ACTIVE:
            if self._stale:
                raise RuntimeError("topology changed; reload required")
            if self._engine is None:
                self._engine = self._make_engine()
            self.adapter.set_active(True)
            self._engine.set_enabled(True)
        else:
            if self._engine is not None:
                self._engine.set_enabled(False)
            self.adapter.set_active(False)
        self.mode = mode
        self._announce(frozenset(light.id for light in self.config.lights))

    def submit(self, light_ids: tuple[str, ...], patch: IntentPatch, *, source: str):
        if self.mode != ControlMode.ACTIVE or self._engine is None or self._closed:
            raise RuntimeError("control unavailable")
        return self._engine.submit(light_ids, patch, source=source)

    def snapshot(self, light_id: str) -> LightSnapshot:
        if self._engine is not None:
            return self._engine.snapshot(light_id)
        return LightSnapshot(
            self._desired.get(light_id),
            Phase.OFF,
            EffectiveState.UNKNOWN,
            False,
            False,
            None,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.adapter.set_active(False)
        if self._remove_topology:
            self._remove_topology()
        if self._engine:
            await self._engine.close()
        await self.adapter.close()
        self._subscribers.clear()
