from __future__ import annotations

import logging
from typing import Any

from models import AlarmEvent, DigitalTelemetryPoint, TagReading, TelemetryPoint

log = logging.getLogger(__name__)

ANALOG = "ANALOG"
DIGITAL = "DIGITAL"


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


class TagChecker:
    """Разделяет считанные значения на ANALOG-телеметрию и DIGITAL-алармы.

    Аларм пишется только на фронт False → True (и только если is_alarm_enabled).
    Первый опрос после старта сервиса не генерирует аларм: состояние запоминается.
    """

    def __init__(self) -> None:
        self._last_digital: dict[int, bool] = {}

    def drop_tag(self, tag_id: int) -> None:
        self._last_digital.pop(tag_id, None)

    def retain_tags(self, tag_ids: set[int]) -> None:
        stale = [tag_id for tag_id in self._last_digital if tag_id not in tag_ids]
        for tag_id in stale:
            self.drop_tag(tag_id)

    def process(
        self, readings: list[TagReading]
    ) -> tuple[
        list[TelemetryPoint],
        list[DigitalTelemetryPoint],
        list[DigitalTelemetryPoint],
        list[AlarmEvent],
    ]:
        telemetry: list[TelemetryPoint] = []
        digital_states: list[DigitalTelemetryPoint] = []
        digital_telemetry: list[DigitalTelemetryPoint] = []
        alarms: list[AlarmEvent] = []

        for reading in readings:
            if not reading.is_good:
                log.debug(
                    "Тег %s (%s): Bad quality, пропуск",
                    reading.tag.name,
                    reading.tag.node_id,
                )
                continue

            tag_type = reading.tag.tag_type
            if tag_type == ANALOG:
                point = self._analog(reading)
                if point is not None:
                    telemetry.append(point)
            elif tag_type == DIGITAL:
                digital_value, changed, initial, event = self._digital(reading)
                if digital_value is not None:
                    point = DigitalTelemetryPoint(
                        plc_id=reading.tag.plc_id,
                        tag_id=reading.tag.id,
                        value=digital_value,
                        ts=reading.source_ts,
                    )
                    if changed or initial:
                        digital_states.append(point)
                    if changed and not initial:
                        digital_telemetry.append(point)
                if event is not None:
                    alarms.append(event)
            else:
                log.warning("Тег %s: неизвестный tag_type=%s", reading.tag.name, tag_type)

        return telemetry, digital_states, digital_telemetry, alarms

    def _analog(self, reading: TagReading) -> TelemetryPoint | None:
        value = _as_float(reading.value)
        if value is None:
            log.warning("ANALOG тег %s: нечисловое значение %r", reading.tag.name, reading.value)
            return None
        return TelemetryPoint(
            plc_id=reading.tag.plc_id,
            tag_id=reading.tag.id,
            value=value,
            ts=reading.source_ts,
        )

    def _digital(
        self, reading: TagReading
    ) -> tuple[bool | None, bool, bool, AlarmEvent | None]:
        current = _as_bool(reading.value)
        if current is None:
            log.warning("DIGITAL тег %s: не булево значение %r", reading.tag.name, reading.value)
            return None, False, False, None

        previous = self._last_digital.get(reading.tag.id)
        self._last_digital[reading.tag.id] = current

        if previous is None:
            return current, False, True, None
        if previous or not current:
            return current, previous != current, False, None
        if not reading.tag.is_alarm_enabled:
            return current, True, False, None

        message = reading.tag.alarm_message or reading.tag.name
        return current, True, False, AlarmEvent(
            plc_id=reading.tag.plc_id,
            tag_id=reading.tag.id,
            alarm_message=message,
            value=True,
            ts=reading.source_ts,
        )
