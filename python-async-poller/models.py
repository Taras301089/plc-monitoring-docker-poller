from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Plc:
    id: int
    name: str
    opc_endpoint: str
    poll_interval_ms: int
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class Tag:
    id: int
    plc_id: int
    name: str
    node_id: str
    tag_type: str
    is_alarm_enabled: bool
    alarm_message: str | None = None


@dataclass(slots=True)
class TagReading:
    tag: Tag
    value: Any
    source_ts: datetime
    is_good: bool


@dataclass(frozen=True, slots=True)
class TelemetryPoint:
    plc_id: int
    tag_id: int
    value: float
    ts: datetime


@dataclass(frozen=True, slots=True)
class DigitalTelemetryPoint:
    plc_id: int
    tag_id: int
    value: bool
    ts: datetime


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    plc_id: int
    tag_id: int
    alarm_message: str
    value: bool
    ts: datetime
