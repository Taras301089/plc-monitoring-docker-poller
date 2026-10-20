"""Сборщик аварий самого ПЛК по OPC UA Alarms & Conditions (A&C) в таблицу alarm_ac_events.

Только чтение: подписка на события условий (AlarmConditionType) у узла Server и ConditionRefresh;
никаких записей в ПЛК и вызовов Acknowledge. Работает для ПЛК с флажками plcs.ac_alarms и plcs.is_active.

У каждого такого ПЛК своё отдельное подключение OPC UA (не общий клиент опроса), поэтому подписка
не мешает опросу тегов и не держит его блокировку. Список ПЛК перечитывается каждые _RELOAD_SEC,
флажок включается и выключается без перезапуска поллера.

При каждом подключении (и после переподключения) вызывается ConditionRefresh: условия между
RefreshStart и RefreshEnd считаются текущим состоянием; открытые в базе условия, которых среди них нет,
закрываются временем получения RefreshEnd. Таблицу создаёт API при запуске; пока её нет, сборщик ждёт."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import asyncpg

log = logging.getLogger(__name__)

_RELOAD_SEC = 10.0
_PUBLISH_MS = 1000             # ПЛК публикует не чаще 200 мс; 1 с достаточно
_HEALTH_SEC = 5.0              # проверка живости сессии (сессия ПЛК живёт не более 30 с без запросов)
_CONNECT_TIMEOUT_SEC = 10.0
_BACKOFF_START_SEC = 2.0
_BACKOFF_MAX_SEC = 60.0

REFRESH_START_TYPE = 2787      # RefreshStartEventType
REFRESH_END_TYPE = 2788        # RefreshEndEventType


# ------------------------------------------------------------------ разбор события (чистые функции)
@dataclass(frozen=True, slots=True)
class AcAction:
    kind: str                       # open / close / ack / refresh_start / refresh_end / ignore
    condition_id: str = ""
    source: str = ""
    condition_name: str = ""
    message: str = ""
    severity: int | None = None
    ts: datetime | None = None      # время ПЛК: начало (open), снятие (close), квитирование (ack)


def _numeric_id(value: Any) -> int | None:
    """Числовой идентификатор NodeId пространства 0 (ua.NodeId, int или строка 'i=2787')."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    ident = getattr(value, "Identifier", None)
    if ident is not None:
        return ident if isinstance(ident, int) and getattr(value, "NamespaceIndex", 0) == 0 else None
    text = str(value)
    if text.startswith("i=") and text[2:].isdigit():
        return int(text[2:])
    return None


def _text(value: Any) -> str:
    """LocalizedText и прочее в строку (у LocalizedText берётся Text)."""
    if value is None:
        return ""
    if hasattr(value, "Text"):
        return value.Text or ""
    return str(value)


def _node_str(value: Any) -> str:
    if value is None:
        return ""
    to_string = getattr(value, "to_string", None)
    return to_string() if callable(to_string) else str(value)


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def condition_key(fields: dict[str, Any]) -> str:
    """ConditionId (NodeId условия); если сервер его не прислал: SourceNode + ConditionName."""
    cid = _node_str(fields.get("NodeId"))
    if cid:
        return cid
    return f"{_node_str(fields.get('SourceNode'))}|{_text(fields.get('ConditionName'))}"


def parse_event(fields: dict[str, Any]) -> list[AcAction]:
    """Поля события (имя поля -> значение, как в asyncua Event) -> действия для базы.

    Активное условие: open (+ ack, если уже квитировано); снятое: close (+ ack); RefreshStart/RefreshEnd:
    служебные метки; событие без ActiveState (не авария) или без условия: ignore."""
    etype = _numeric_id(fields.get("EventType"))
    if etype == REFRESH_START_TYPE:
        return [AcAction("refresh_start")]
    if etype == REFRESH_END_TYPE:
        return [AcAction("refresh_end")]
    active = fields.get("ActiveState/Id")
    if not isinstance(active, bool):
        return [AcAction("ignore")]
    common = dict(
        condition_id=condition_key(fields),
        source=_text(fields.get("SourceName")),
        condition_name=_text(fields.get("ConditionName")),
        message=_text(fields.get("Message")),
        severity=int(fields["Severity"]) if isinstance(fields.get("Severity"), int) else None,
    )
    if common["condition_id"] in ("", "|"):
        return [AcAction("ignore")]
    event_time = _utc(fields.get("Time"))
    actions = [AcAction(
        "open" if active else "close",
        ts=_utc(fields.get("ActiveState/TransitionTime")) or event_time,
        **common,
    )]
    if fields.get("AckedState/Id") is True:
        actions.append(AcAction("ack", ts=_utc(fields.get("AckedState/TransitionTime")) or event_time, **common))
    return actions


def event_fields(event: Any) -> dict[str, Any]:
    """asyncua Event -> {имя поля: значение}; имена как в Event (ActiveState/Id, NodeId для ConditionId)."""
    clauses = getattr(event, "select_clauses", None)
    values = getattr(event, "event_fields", None)
    if not clauses or values is None:
        return {k: v for k, v in vars(event).items() if not k.startswith("_")} if hasattr(event, "__dict__") else {}
    out: dict[str, Any] = {}
    for clause, variant in zip(clauses, values):
        path = getattr(clause, "BrowsePath", None) or []
        name = "/".join(q.Name for q in path) if path else clause.AttributeId.name
        out[name] = getattr(variant, "Value", variant)
    return out


# ------------------------------------------------------------------ запись в базу
_INSERT_OPEN = """
INSERT INTO alarm_ac_events (plc_id, condition_id, source, condition_name, message, severity, started_at, active_at_start)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (plc_id, condition_id) WHERE ended_at IS NULL DO NOTHING
"""
_CLOSE = """
UPDATE alarm_ac_events SET ended_at = GREATEST($3, started_at)
WHERE plc_id = $1 AND condition_id = $2 AND ended_at IS NULL
"""
# квитирование: открытая строка условия, иначе последняя закрытая (квитировали уже после снятия)
_ACK = """
UPDATE alarm_ac_events SET acked_at = $3
WHERE id = (SELECT id FROM alarm_ac_events WHERE plc_id = $1 AND condition_id = $2
            ORDER BY (ended_at IS NULL) DESC, started_at DESC, id DESC LIMIT 1)
  AND acked_at IS NULL
"""
_CLOSE_MISSING = """
UPDATE alarm_ac_events SET ended_at = GREATEST($3, started_at)
WHERE plc_id = $1 AND ended_at IS NULL AND NOT (condition_id = ANY($2::text[]))
"""
_LOAD_PLCS = """
SELECT id, name, opc_endpoint, username, password FROM plcs
WHERE is_active AND ac_alarms ORDER BY id
"""


async def apply_action(pool: asyncpg.Pool, plc_id: int, action: AcAction, now: datetime, at_start: bool = False) -> None:
    """Пишет одно действие условия: open (без дубля открытой строки), close, ack. Время ПЛК, иначе время получения."""
    ts = action.ts or now
    if action.kind == "open":
        await pool.execute(_INSERT_OPEN, plc_id, action.condition_id, action.source, action.condition_name,
                           action.message, action.severity, ts, at_start)
    elif action.kind == "close":
        await pool.execute(_CLOSE, plc_id, action.condition_id, ts)
    elif action.kind == "ack":
        await pool.execute(_ACK, plc_id, action.condition_id, ts)


async def close_missing(pool: asyncpg.Pool, plc_id: int, active_ids: set[str], now: datetime) -> int:
    """После RefreshEnd: открытые в базе условия, которых нет среди активных, закрываются временем получения."""
    status = await pool.execute(_CLOSE_MISSING, plc_id, sorted(active_ids), now)
    return int(status.split()[-1]) if status else 0


class PlcAcState:
    """Состояние разбора событий одного ПЛК: идёт ли сейчас ConditionRefresh и какие условия в нём активны."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool], plc_id: int) -> None:
        self._get_pool = pool_getter
        self.plc_id = plc_id
        self.in_refresh = False
        self.refresh_active: set[str] = set()

    async def handle(self, fields: dict[str, Any], now: datetime | None = None) -> list[AcAction]:
        now = now or datetime.now(timezone.utc)
        pool = self._get_pool()
        actions = parse_event(fields)
        for action in actions:
            if action.kind == "refresh_start":
                self.in_refresh = True
                self.refresh_active = set()
            elif action.kind == "refresh_end":
                if self.in_refresh:
                    closed = await close_missing(pool, self.plc_id, self.refresh_active, now)
                    if closed:
                        log.info("ПЛК id=%s: A&C refresh закрыл %s пропавших аварий", self.plc_id, closed)
                self.in_refresh = False
                self.refresh_active = set()
            elif action.kind == "open":
                if self.in_refresh:
                    self.refresh_active.add(action.condition_id)
                await apply_action(pool, self.plc_id, action, now, at_start=self.in_refresh)
            elif action.kind in ("close", "ack"):
                await apply_action(pool, self.plc_id, action, now)
        return actions


# ------------------------------------------------------------------ подписка на ПЛК
class _Handler:
    """Обработчик подписки asyncua: события кладутся в очередь, разбор и запись идут в задаче ПЛК."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def event_notification(self, event: Any) -> None:
        try:
            self._queue.put_nowait(("event", event_fields(event), datetime.now(timezone.utc)))
        except Exception:
            log.exception("A&C: событие не разобрано")

    def status_change_notification(self, status: Any) -> None:
        self._queue.put_nowait(("lost", status, datetime.now(timezone.utc)))

    def datachange_notification(self, *args: Any) -> None:  # не используется
        pass


class AcCollector:
    def __init__(self, get_pool: Callable[[], asyncpg.Pool], stop: asyncio.Event) -> None:
        self._get_pool = get_pool
        self._stop = stop
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._config: dict[int, tuple] = {}
        self._warned = False

    async def run(self) -> None:
        log.info("Сборщик аварий ПЛК (OPC UA A&C) запущен")
        try:
            while not self._stop.is_set():
                try:
                    await self._reconcile()
                except Exception:
                    log.exception("Сбой сборщика A&C")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=_RELOAD_SEC)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_all()

    async def _load(self) -> list[asyncpg.Record] | None:
        try:
            return await self._get_pool().fetch(_LOAD_PLCS)
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
            if not self._warned:
                log.warning("Колонки plcs.ac_alarms ещё нет (её создаёт API при запуске)")
                self._warned = True
            return None

    async def _reconcile(self) -> None:
        rows = await self._load()
        if rows is None:
            return
        wanted = {r["id"]: (r["name"], r["opc_endpoint"], r["username"], r["password"]) for r in rows}
        for plc_id in list(self._tasks):
            task = self._tasks[plc_id]
            if plc_id not in wanted or wanted[plc_id] != self._config.get(plc_id) or task.done():
                await self._stop_one(plc_id)
        for plc_id, cfg in wanted.items():
            if plc_id not in self._tasks:
                self._config[plc_id] = cfg
                self._tasks[plc_id] = asyncio.create_task(self._plc_loop(plc_id, cfg), name=f"ac-{plc_id}")
                log.info("A&C: сбор аварий ПЛК %s (id=%s) включён", cfg[0], plc_id)

    async def _stop_one(self, plc_id: int) -> None:
        task = self._tasks.pop(plc_id, None)
        self._config.pop(plc_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _stop_all(self) -> None:
        for plc_id in list(self._tasks):
            await self._stop_one(plc_id)

    async def _plc_loop(self, plc_id: int, cfg: tuple) -> None:
        backoff = _BACKOFF_START_SEC
        while not self._stop.is_set():
            try:
                await self._session(plc_id, cfg)
                backoff = _BACKOFF_START_SEC
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ПЛК %s: подписка A&C прервана (%s: %s). Повтор через %.0f с",
                            cfg[0], exc.__class__.__name__, exc, backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, _BACKOFF_MAX_SEC)

    async def _session(self, plc_id: int, cfg: tuple) -> None:
        """Одна сессия: подключение, подписка, ConditionRefresh, обработка событий до обрыва связи."""
        from asyncua import Client, ua

        name, endpoint, username, password = cfg
        client = Client(url=endpoint, timeout=_CONNECT_TIMEOUT_SEC)
        if username:
            client.set_user(username)
            client.set_password(password or "")
        queue: asyncio.Queue = asyncio.Queue()
        state = PlcAcState(self._get_pool, plc_id)
        await client.connect()
        try:
            sub = await client.create_subscription(_PUBLISH_MS, _Handler(queue))
            await sub.subscribe_alarms_and_conditions(
                client.get_node(ua.ObjectIds.Server), client.get_node(ua.ObjectIds.AlarmConditionType)
            )
            await client.get_node(ua.ObjectIds.ConditionType).call_method(
                ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh),
                ua.Variant(sub.subscription_id, ua.VariantType.UInt32),
            )
            log.info("ПЛК %s: подписка A&C активна, ConditionRefresh запрошен", name)
            while not self._stop.is_set():
                try:
                    kind, payload, received = await asyncio.wait_for(queue.get(), timeout=_HEALTH_SEC)
                except asyncio.TimeoutError:
                    # живость сессии: чтение ServerStatus/State (только чтение)
                    await asyncio.wait_for(client.get_node(ua.ObjectIds.Server_ServerStatus_State).read_value(),
                                           timeout=_CONNECT_TIMEOUT_SEC)
                    continue
                if kind == "lost":
                    raise ConnectionError(f"статус подписки: {payload}")
                try:
                    await state.handle(payload, received)
                except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                    log.warning("ПЛК %s: таблицы alarm_ac_events ещё нет, событие A&C пропущено", name)
                except (OSError, asyncpg.PostgresError):
                    log.exception("ПЛК %s: событие A&C не записано в базу", name)
        finally:
            with suppress(Exception):
                await asyncio.wait_for(client.disconnect(), timeout=_CONNECT_TIMEOUT_SEC)
