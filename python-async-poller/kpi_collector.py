"""Сборщик истории KPI: раз в минуту сохраняет почасовые счётчики кузовов экранов Andon в таблицу kpi_hourly."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import asyncpg

from opc_client import PlcOpcClient

log = logging.getLogger(__name__)

_COLLECT_SEC = max(int(os.getenv("KPI_COLLECT_SEC", "60")), 10)
# Смещение местного времени производства относительно UTC (в контейнере время UTC)
_UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))

# Счётчики в течение суток только растут: значение не уменьшается (GREATEST), поэтому сброс массивов
# в ПЛК (нули после смены или суток) не затирает уже накопленное за день
_UPSERT = """
INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact, takt_sec)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (screen_id, prod_date, idx) DO UPDATE SET
    start_min  = EXCLUDED.start_min,
    end_min    = CASE WHEN EXCLUDED.fact >= kpi_hourly.fact THEN EXCLUDED.end_min ELSE kpi_hourly.end_min END,
    plan       = CASE WHEN EXCLUDED.fact >= kpi_hourly.fact THEN EXCLUDED.plan ELSE kpi_hourly.plan END,
    takt_sec   = CASE WHEN EXCLUDED.fact > kpi_hourly.fact
                        OR (EXCLUDED.fact = kpi_hourly.fact AND EXCLUDED.fact > 0)
                      THEN EXCLUDED.takt_sec ELSE kpi_hourly.takt_sec END,
    updated_at = CASE WHEN EXCLUDED.fact >= kpi_hourly.fact THEN now() ELSE kpi_hourly.updated_at END,
    fact       = GREATEST(kpi_hourly.fact, EXCLUDED.fact)
"""


def production_date(first_start_min: int, now_utc: datetime | None = None) -> date:
    """Производственные сутки начинаются с первого интервала смены, а не в полночь."""
    now = now_utc or datetime.now(timezone.utc)
    local = now + timedelta(hours=_UTC_OFFSET_HOURS)
    return (local - timedelta(minutes=first_start_min)).date()


_EVENT_POLL_SEC = max(float(os.getenv("KPI_EVENT_POLL_SEC", "1")), 0.5)
_INSERT_EVENT = """
INSERT INTO kpi_body_events
    (screen_id, prod_date, interval_idx, kind, model, model_now, model_age_sec, delta, total_after)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""


def process_event_values(
    state: dict[str, Any], tot: Any, model_raw: Any, idx: Any, starts: Any, now_mono: float
) -> list[tuple[Any, ...]]:
    """Один шаг журнала: по новым значениям (счётчик, модель на считывателе) возвращает события для записи.

    Кортеж: (kind, interval_idx, model, model_now, model_age_sec, delta, total_after, first_start_min).
    kind: 'model' (на считывателе появилась другая модель), 'body' (счётчик вырос), 'reset' (счётчик уменьшился).
    """
    if not isinstance(tot, int) or isinstance(tot, bool):
        return []
    model = model_raw.replace("\x00", "").strip() if isinstance(model_raw, str) else ""
    first_start = int(starts[0]) if isinstance(starts, list) and starts else 450
    interval = int(idx) if isinstance(idx, int) and not isinstance(idx, bool) else None
    events: list[tuple[Any, ...]] = []
    if model:
        if model != state.get("last_model"):
            events.append(("model", interval, model, model, 0, 0, tot, first_start))
        state["last_model"] = model
        state["last_seen"] = now_mono
    prev = state.get("total")
    if prev is None:
        state["total"] = tot
        return events
    if tot < prev:
        events.append(("reset", interval, None, model or None, None, 0, tot, first_start))
    elif tot > prev:
        last_seen = state.get("last_seen")
        age = int(now_mono - last_seen) if last_seen is not None else None
        events.append(("body", interval, state.get("last_model") or None, model or None, age, tot - prev, tot, first_start))
    state["total"] = tot
    return events


class BodyEventLogger:
    """Журнал: каждый кузов, посчитанный счётчиком бренда, с моделью, считанной на RFID (для GWM: A01 / P01)."""

    def __init__(
        self,
        get_pool: Callable[[], asyncpg.Pool],
        get_clients: Callable[[], dict[int, PlcOpcClient]],
        stop: asyncio.Event,
    ) -> None:
        self._get_pool = get_pool
        self._get_clients = get_clients
        self._stop = stop
        self._state: dict[int, dict[str, Any]] = {}
        self._screens: list[tuple[int, int, dict[str, str]]] = []
        self._loaded_at = 0.0
        self._warned = False

    async def run(self) -> None:
        log.info("Журнал событий кузовов запущен: опрос каждые %.1f с", _EVENT_POLL_SEC)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("Сбой журнала событий кузовов")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_EVENT_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _load_screens(self) -> None:
        pool = self._get_pool()
        try:
            rows = await pool.fetch("SELECT id, plc_id, bindings FROM kpi_screens WHERE template = 'body_counter'")
        except asyncpg.UndefinedTableError:
            self._screens = []
            return
        screens = []
        for r in rows:
            raw = r["bindings"]
            b = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            if b.get("tot") and b.get("model"):
                screens.append((r["id"], r["plc_id"], b))
        self._screens = screens

    async def _tick(self) -> None:
        loop_now = asyncio.get_running_loop().time()
        if loop_now - self._loaded_at > 30:
            await self._load_screens()
            self._loaded_at = loop_now
        if not self._screens:
            return
        clients = self._get_clients()
        by_plc: dict[int, list[tuple[int, dict[str, str]]]] = defaultdict(list)
        for sid, plc_id, b in self._screens:
            by_plc[plc_id].append((sid, b))
        for plc_id, items in by_plc.items():
            client = clients.get(plc_id)
            if client is None:
                continue
            node_ids = sorted({b[k] for _, b in items for k in ("tot", "model", "idx", "starts") if b.get(k)})
            try:
                values = await client.read_values(node_ids)
            except Exception:
                continue
            for sid, b in items:
                def val(role: str, b: dict[str, str] = b) -> Any:
                    v = values.get(b.get(role, ""))
                    return v["v"] if v and v.get("ok") else None

                state = self._state.setdefault(sid, {})
                for kind, interval, model, model_now, age, delta, total_after, first_start in process_event_values(
                    state, val("tot"), val("model"), val("idx"), val("starts"), loop_now
                ):
                    try:
                        await self._get_pool().execute(
                            _INSERT_EVENT, sid, production_date(first_start), interval, kind, model, model_now, age, delta, total_after
                        )
                    except asyncpg.UndefinedTableError:
                        if not self._warned:
                            log.warning("Таблица kpi_body_events ещё не создана (её создаёт API при запуске)")
                            self._warned = True


class KpiCollector:
    def __init__(
        self,
        get_pool: Callable[[], asyncpg.Pool],
        get_clients: Callable[[], dict[int, PlcOpcClient]],
        stop: asyncio.Event,
    ) -> None:
        self._get_pool = get_pool
        self._get_clients = get_clients
        self._stop = stop
        self._schema_warned = False

    async def run(self) -> None:
        log.info("Сборщик KPI запущен: период %s с, смещение времени UTC%+g ч", _COLLECT_SEC, _UTC_OFFSET_HOURS)
        while not self._stop.is_set():
            try:
                await self._cycle()
            except Exception:
                log.exception("Сбой цикла сборщика KPI")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_COLLECT_SEC)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        pool = self._get_pool()
        try:
            screens = await pool.fetch(
                "SELECT id, plc_id, name, bindings FROM kpi_screens WHERE template = 'body_counter'"
            )
            if not screens:
                return
            sched_rows = await pool.fetch(
                "SELECT screen_id, idx, end_min, plan FROM kpi_schedule WHERE screen_id = ANY($1::int[])",
                [s["id"] for s in screens],
            )
        except asyncpg.UndefinedTableError:
            if not self._schema_warned:
                log.warning("Таблицы KPI ещё не созданы (их создаёт API при запуске), сборщик подождёт")
                self._schema_warned = True
            return
        self._schema_warned = False

        sched: dict[int, dict[int, tuple[int, int]]] = defaultdict(dict)
        for r in sched_rows:
            sched[r["screen_id"]][r["idx"]] = (r["end_min"], r["plan"])

        by_plc: dict[int, list[tuple[asyncpg.Record, dict[str, str]]]] = defaultdict(list)
        for s in screens:
            raw = s["bindings"]
            bindings = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            if bindings.get("prod") and bindings.get("starts"):
                by_plc[s["plc_id"]].append((s, bindings))

        clients = self._get_clients()
        for plc_id, items in by_plc.items():
            client = clients.get(plc_id)
            if client is None:
                continue
            node_ids = sorted({nid for _, b in items for nid in b.values()})
            try:
                values = await client.read_values(node_ids)
            except Exception as exc:
                log.warning("KPI: не удалось прочитать ПЛК %s: %s", plc_id, exc)
                continue
            for screen, bindings in items:
                await self._store(pool, screen, bindings, values, sched.get(screen["id"], {}))

    async def _store(
        self,
        pool: asyncpg.Pool,
        screen: asyncpg.Record,
        bindings: dict[str, str],
        values: dict[str, dict[str, Any]],
        sched: dict[int, tuple[int, int]],
    ) -> None:
        def val(role: str) -> Any:
            v = values.get(bindings.get(role, ""))
            return v["v"] if v and v.get("ok") else None

        prod, starts = val("prod"), val("starts")
        if not isinstance(prod, list) or not isinstance(starts, list) or not prod or not starts:
            return
        t_min = val("tMin") if isinstance(val("tMin"), list) else []
        t_sec = val("tSec") if isinstance(val("tSec"), list) else []
        pdate = production_date(int(starts[0]))
        rows = []
        for i, fact in enumerate(prod[: len(starts)]):
            fact = int(fact or 0)
            default_end = int(starts[i + 1]) if i + 1 < len(starts) else int(starts[i]) + 30
            end, plan = sched.get(i + 1, (default_end, 0))
            takt = None
            if fact > 0 and i < len(t_min) and i < len(t_sec):
                takt = int(t_min[i] or 0) * 60 + int(t_sec[i] or 0)
            rows.append((screen["id"], pdate, i + 1, int(starts[i]), int(end), int(plan), fact, takt))
        try:
            await pool.executemany(_UPSERT, rows)
        except asyncpg.UndefinedTableError:
            if not self._schema_warned:
                log.warning("Таблица kpi_hourly ещё не создана (её создаёт API при запуске)")
                self._schema_warned = True
