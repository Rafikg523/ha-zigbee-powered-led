"""Home Assistant independent domain types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TypeAlias


class Phase(StrEnum):
    OFF = "OFF"
    POWERING = "POWERING"
    WAITING_READY = "WAITING_READY"
    APPLYING = "APPLYING"
    ON = "ON"
    UNCONFIRMED = "UNCONFIRMED"
    ERROR = "ERROR"


class EffectiveState(StrEnum):
    OFF_CONFIRMED = "OFF_CONFIRMED"
    ON_REPORTED = "ON_REPORTED"
    UNKNOWN = "UNKNOWN"


class SwitchState(StrEnum):
    ON = "ON"
    OFF = "OFF"
    UNKNOWN = "UNKNOWN"


class EvidenceQuality(StrEnum):
    CACHE = "CACHE"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
    DEVICE_REPORT = "DEVICE_REPORT"


class ControlMode(StrEnum):
    OBSERVE = "OBSERVE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class CommandPriority(IntEnum):
    OFF = 0
    NORMAL = 1


class CommandKind(StrEnum):
    RELAY_ON = "RELAY_ON"
    RELAY_OFF = "RELAY_OFF"
    LED_SET = "LED_SET"
    LED_OFF = "LED_OFF"
    GROUP_SET = "GROUP_SET"
    PROBE = "PROBE"


class SendStatus(StrEnum):
    SENT = "SENT"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class IntentPatch:
    on: bool | None = None
    brightness: int | None = None
    color_temp_kelvin: int | None = None

    def __post_init__(self) -> None:
        if self.on is not None and not isinstance(self.on, bool):
            raise ValueError("on must be a boolean")
        if self.brightness is not None:
            if type(self.brightness) is not int or not 0 <= self.brightness <= 255:
                raise ValueError("brightness must be an integer in 0..255")
            if self.brightness == 0:
                object.__setattr__(self, "on", False)
                object.__setattr__(self, "brightness", None)
        if self.color_temp_kelvin is not None and (
            type(self.color_temp_kelvin) is not int or self.color_temp_kelvin <= 0
        ):
            raise ValueError("color_temp_kelvin must be a positive integer")


@dataclass(frozen=True, slots=True)
class DesiredState:
    on: bool
    brightness: int | None
    color_temp_kelvin: int | None
    generation: int
    source: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class CommandToken:
    light_id: str
    generation: int
    power_epoch: int
    bridge_epoch: int
    control_epoch: int


def _validate_json(value: object, active: set[int]) -> None:
    """Reject coercions and circular references before serializing a command."""
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) not in (dict, list):
        raise ValueError("payload must contain only JSON values")
    identity = id(value)
    if identity in active:
        raise ValueError("payload must not contain circular references")
    active.add(identity)
    try:
        if isinstance(value, dict):
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("payload object keys must be strings")
                _validate_json(child, active)
        else:
            for child in value:
                _validate_json(child, active)
    finally:
        active.remove(identity)


@dataclass(frozen=True, slots=True, init=False)
class SendRequest:
    """An immutable command whose payload accessor returns an independent copy."""

    topic: str
    _payload_json: str = field(repr=False)
    tokens: tuple[CommandToken, ...]
    resources: tuple[str, ...]
    priority: CommandPriority
    operation_id: str
    kind: CommandKind
    topology_version: int

    def __init__(
        self,
        topic: str,
        payload: dict[str, object],
        tokens: tuple[CommandToken, ...],
        resources: tuple[str, ...],
        priority: CommandPriority,
        operation_id: str,
        kind: CommandKind,
        topology_version: int,
    ) -> None:
        if type(payload) is not dict:
            raise ValueError("payload must be a JSON object")
        _validate_json(payload, set())
        try:
            encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("payload must contain finite JSON values") from exc
        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "_payload_json", encoded)
        object.__setattr__(self, "tokens", tuple(tokens))
        object.__setattr__(self, "resources", tuple(resources))
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "topology_version", topology_version)

    @property
    def payload(self) -> dict[str, object]:
        """Return a JSON-compatible copy, never the queued command's own data."""
        return json.loads(self._payload_json)


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RelayObservation:
    channel_id: str
    state: SwitchState
    received_at: float
    retained: bool
    quality: EvidenceQuality


@dataclass(frozen=True, slots=True)
class LedObservation:
    light_id: str
    state: SwitchState
    received_at: float
    retained: bool
    quality: EvidenceQuality
    brightness: int | None = None
    color_temp_kelvin: int | None = None


@dataclass(frozen=True, slots=True)
class BridgeObservation:
    online: bool
    received_at: float
    retained: bool
    quality: EvidenceQuality


Observation: TypeAlias = RelayObservation | LedObservation | BridgeObservation  # noqa: UP040


@dataclass(frozen=True, slots=True)
class ObservationDelta:
    light_ids: frozenset[str]
    bridge_changed: bool = False


@dataclass(frozen=True, slots=True)
class LightObservation:
    power_epoch: int
    bridge_epoch: int
    relay_state: SwitchState
    led_state: SwitchState
    relay_quality: EvidenceQuality
    led_quality: EvidenceQuality
    relay_retained: bool | None = None
    led_retained: bool | None = None
    brightness: int | None = None
    color_temp_kelvin: int | None = None
    relay_received_at: float | None = None
    led_received_at: float | None = None
    power_started_at: float | None = None
    # Receipt time of a trusted ON→OFF report, not a physical switching timestamp.
    power_off_at: float | None = None


@dataclass(frozen=True, slots=True)
class LightSnapshot:
    desired: DesiredState
    phase: Phase
    effective: EffectiveState
    confirmed: bool
    available: bool
    reason: str | None


class GroupAction(StrEnum):
    GROUP_SENT = "GROUP_SENT"
    INDIVIDUAL = "INDIVIDUAL"
    ABORT = "ABORT"


@dataclass(frozen=True, slots=True)
class GroupDecision:
    """Barrier outcome for one member; GROUP_SENT is a publication, not an ACK."""

    action: GroupAction
    sent_at: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    """Group state derived from individual members, never from group reports."""

    on: bool
    confirmed_members: int
    member_count: int
    confirmed: bool
    available: bool


@dataclass(frozen=True, slots=True)
class RelayChannelConfig:
    id: str
    device_ieee: str
    endpoint: int
    state_key: str


@dataclass(frozen=True, slots=True)
class LightConfig:
    id: str
    device_ieee: str
    endpoint: int
    power_channel: str
    min_kelvin: int
    max_kelvin: int


@dataclass(frozen=True, slots=True)
class GroupConfig:
    id: str
    z2m_id: int
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomConfig:
    id: str
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    min_send_interval_ms: int
    publication_timeout_ms: int
    min_power_off_ms: int = 400
    boot_grace_ms: int = 2500
    readiness_timeout_ms: int = 8000
    group_barrier_timeout_ms: int = 8000
    slider_coalesce_ms: int = 120
    verification_timeout_ms: int = 2000
    relay_command_timeout_ms: int = 3000
    z2m_set_retain: bool = False
    startup_restore_power: bool = False
    restore_parameters: bool = False


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    base_topic: str
    relay_channels: tuple[RelayChannelConfig, ...]
    lights: tuple[LightConfig, ...]
    groups: tuple[GroupConfig, ...]
    rooms: tuple[RoomConfig, ...]
    policies: PolicyConfig
    report_extension: bool = False
