"""Циклы передачи кузова лифтом (сначала EL300 на WBS1): начало, конец, ожидание разрешения и временные диаграммы сигналов.

Единый расчёт: экран «временные диаграммы циклов» и будущие отчёты вызывают find_cycles / intervals, а не считают сами.
Данные только читаются: digital_telemetry (только изменения сигналов) и digital_tag_state (текущее значение).
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import require_user

router = APIRouter(prefix="/api/lifts")

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
MAX_PERIOD_DAYS = 31
MAX_CYCLES = 5000          # больше циклов в ответе не отдаём (флаг truncated)
PRE_S = 15                 # окно диаграммы: столько секунд до начала цикла
POST_S = 30                # «досмотр» после конца цикла
OPEN_VIEW_S = 300          # цикл без конца: на диаграмме показываем столько секунд после начала
DETAIL_HORIZON_S = 3600    # подробности цикла: конец ищется не дальше этого от начала
DEFAULT_THRESHOLD_S = 10.0

# Лифты: ПЛК (по имени), условие начала, сигналы конца, сигналы цепочки разрешения (порядок для диаграммы)
# и те из них, которые считаются разрешением (из них выбирается last_signal). Новый лифт добавляется сюда же.
LIFTS: dict[str, dict[str, Any]] = {
    "EL300": {
        "plc": "AE_WBS_1",
        "start_all": ["OPMode08_DB.EL300.At2nd", "OPMode08_DB.FK300.AtTop"],   # начало: передний фронт «И»
        "at_station": "OPMode08_DB.EL300.At2nd",                              # ушёл отсюда без разрешения: цикл прерван
        "end_any": ["OPMode08_DB.EL300.Goto3rd", "OPMode08_DB.EL300.Goto4th"],  # конец: первый передний фронт любого
        "signals": [
            "OPMode08_DB.EL300.At2nd",
            "OPMode08_DB.FK300.AtTop",
            "ITL_GWM_DB.From GWM.BodyCheck1",
            "ITL_GWM_DB.From GWM.SHT_Inposition",
            "ITL_GWM_DB.From GWM.RB_Inposition",
            "ITL_GWM_DB.From GWM.POS_Closed",
            "ITL_GWM_DB.From GWM.ReqRevCar",
            "ITL_GWM_DB.From GWM.ReqCheck",
            "ITL_GWM_DB.RevNewData",
            "ITL_GWM_DB.From GWM.Auto",
            "OPMode08_DB.FK300.Nobody",
            "OPMode08_DB.EL300.At3rd",
            "OPMode08_DB.EL300.Fault",
            "OPMode08_DB.EL300.Goto3rd",
            "OPMode08_DB.EL300.Goto4th",
        ],
        # разрешения: положение, проверки кузова, обмен с GWM, режим и «никого нет» (без At3rd, Fault и самих Goto)
        "permits": [
            "OPMode08_DB.EL300.At2nd",
            "OPMode08_DB.FK300.AtTop",
            "ITL_GWM_DB.From GWM.BodyCheck1",
            "ITL_GWM_DB.From GWM.SHT_Inposition",
            "ITL_GWM_DB.From GWM.RB_Inposition",
            "ITL_GWM_DB.From GWM.POS_Closed",
            "ITL_GWM_DB.From GWM.ReqRevCar",
            "ITL_GWM_DB.From GWM.ReqCheck",
            "ITL_GWM_DB.RevNewData",
            "ITL_GWM_DB.From GWM.Auto",
            "OPMode08_DB.FK300.Nobody",
        ],
    },
}

# Ряд сигнала: (значение на начало окна, изменения [(ts, value)] по возрастанию времени внутри окна)
Series = tuple[bool | None, list[tuple[datetime, bool]]]


# ---------- чистые функции (без базы) ----------

def initial_value(last_before: bool | None, first_after: bool | None, current: bool | None) -> bool | None:
    """Значение сигнала на момент T: последнее изменение до T; изменений до T нет, но есть после:
    противоположное первому изменению после T (в telemetry пишутся только изменения); изменений нет вообще: текущее."""
    if last_before is not None:
        return bool(last_before)
    if first_after is not None:
        return not bool(first_after)
    return None if current is None else bool(current)


def edges(series: Series) -> list[tuple[datetime, bool]]:
    """Только настоящие переходы (повтор того же значения переходом не считается): [(ts, новое значение)]."""
    val, changes = series
    out = []
    for ts, v in changes:
        v = bool(v)
        if v != val:
            out.append((ts, v))
            val = v
    return out


def value_at(series: Series, t: datetime) -> bool:
    """Значение ряда в момент t (неизвестное начальное значение считается 0)."""
    val = bool(series[0])
    for ts, v in series[1]:
        if ts > t:
            break
        val = bool(v)
    return val


def signal_intervals(series: Series, t0: datetime, t1: datetime) -> list[list[datetime]]:
    """Отрезки [from, to], когда сигнал = 1 внутри окна [t0, t1] (обрезаны по окну)."""
    out: list[list[datetime]] = []
    on = value_at(series, t0)
    since = t0 if on else None
    for ts, v in edges(series):
        if ts <= t0:
            continue
        if ts >= t1:
            break
        if v and not on:
            since, on = ts, True
        elif not v and on:
            out.append([since, ts])
            since, on = None, False
    if on:
        out.append([since, t1])
    return out


def _and_rising(series: dict[str, Series], names: list[str], t0: datetime) -> list[datetime]:
    """Передние фронты условия «все сигналы names = 1» (одновременные изменения применяются вместе)."""
    state = {n: value_at(series[n], t0) for n in names}
    cur = all(state.values())
    events = sorted((ts, n, v) for n in names for ts, v in edges(series[n]) if ts > t0)
    out: list[datetime] = []
    i = 0
    while i < len(events):
        ts = events[i][0]
        while i < len(events) and events[i][0] == ts:
            state[events[i][1]] = events[i][2]
            i += 1
        now = all(state.values())
        if now and not cur:
            out.append(ts)
        cur = now
    return out


def _first_edge(series: Series, after: datetime, until: datetime, rising: bool) -> datetime | None:
    for ts, v in edges(series):
        if after < ts <= until and v == rising:
            return ts
    return None


def last_permit(series: dict[str, Series], permits: list[str], start: datetime, end: datetime) -> tuple[str | None, datetime | None]:
    """Какой сигнал разрешения последним перешёл в 1 в [start, end] (при равном времени: первый по порядку цепочки)."""
    best: tuple[str | None, datetime | None] = (None, None)
    for n in permits:
        if n not in series:
            continue
        for ts, v in edges(series[n]):
            if v and start <= ts <= end and (best[1] is None or ts > best[1]):
                best = (n, ts)
    return best


def find_cycles(series: dict[str, Series], cfg: dict[str, Any], t0: datetime, t1: datetime,
                threshold_s: float = DEFAULT_THRESHOLD_S) -> list[dict[str, Any]]:
    """Циклы лифта, начавшиеся в [t0, t1). series: ряды всех сигналов cfg (значение на t0 и изменения до t1).

    Начало: передний фронт «И» сигналов start_all. Конец: первый передний фронт любого из end_any после начала.
    Лифт ушёл с позиции (at_station = 0) раньше конца: status 'aborted', end = null.
    Конца нет до t1: status 'open'. Иначе wait_s = end - start, status 'ok' (wait_s <= threshold) или 'problem'.
    Пока цикл не закончен, новое начало не считается (циклы не перекрываются)."""
    out: list[dict[str, Any]] = []
    busy_until: datetime | None = None
    for start in _and_rising(series, cfg["start_all"], t0):
        if start >= t1:
            break
        if busy_until is not None and start < busy_until:
            continue
        # Разрешение включилось в тот же опрос, что и начало (сигналы от линии пришли заранее): ожидание 0 с
        if any(value_at(series[n], start) for n in cfg["end_any"]):
            end = start
        else:
            ends = [e for e in (_first_edge(series[n], start, t1, True) for n in cfg["end_any"]) if e is not None]
            end = min(ends) if ends else None
        left = _first_edge(series[cfg["at_station"]], start, t1, False)
        c: dict[str, Any] = {"start": start, "end": None, "wait_s": None, "status": "open",
                             "last_signal": None, "last_signal_ts": None, "aborted_ts": None}
        if left is not None and (end is None or left < end):
            c.update(status="aborted", aborted_ts=left)
            busy_until = left
        elif end is not None:
            wait = (end - start).total_seconds()
            # последний сигнал ищется и немного до начала: при ожидании 0 с сигналы линии пришли раньше вил
            name, ts = last_permit(series, cfg["permits"], start - timedelta(seconds=PRE_S), end)
            c.update(end=end, wait_s=round(wait, 3), status="ok" if wait <= threshold_s else "problem",
                     last_signal=name, last_signal_ts=ts)
            busy_until = end
        else:
            busy_until = t1
        out.append(c)
    return out


def view_window(cycle: dict[str, Any]) -> tuple[datetime, datetime]:
    """Окно диаграммы цикла: PRE_S до начала .. POST_S после конца (прерван: после ухода; без конца: OPEN_VIEW_S)."""
    tail = cycle.get("end") or cycle.get("aborted_ts") or cycle["start"] + timedelta(seconds=OPEN_VIEW_S)
    return cycle["start"] - timedelta(seconds=PRE_S), tail + timedelta(seconds=POST_S)


def intervals(series: dict[str, Series], signals: list[str], cycle: dict[str, Any]) -> dict[str, list[list[datetime]]]:
    """Для каждого сигнала отрезки [from, to], когда он = 1 в окне цикла (view_window)."""
    w0, w1 = view_window(cycle)
    return {n: signal_intervals(series[n], w0, w1) if n in series else [] for n in signals}


def summary(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c["wait_s"] for c in cycles if c["status"] == "ok"]
    return {
        "count": len(cycles),
        "ok": len(ok),
        "problem": sum(1 for c in cycles if c["status"] == "problem"),
        "open": sum(1 for c in cycles if c["status"] == "open"),
        "aborted": sum(1 for c in cycles if c["status"] == "aborted"),
        "avg_wait_ok_s": round(sum(ok) / len(ok), 2) if ok else None,
    }


# ---------- загрузка из базы (только чтение) ----------

def _tz() -> timezone:
    return timezone(timedelta(hours=UTC_OFFSET_HOURS))


def period_bounds(d1: date, d2: date) -> tuple[datetime, datetime]:
    """Границы периода по времени завода: с 00:00 первого дня до 00:00 дня после последнего."""
    tz = _tz()
    return datetime.combine(d1, time(), tz), datetime.combine(d2 + timedelta(days=1), time(), tz)


def _cfg(lift: str) -> dict[str, Any]:
    cfg = LIFTS.get(lift)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Неизвестный лифт: {lift}")
    return cfg


async def load_series(pool: asyncpg.Pool, cfg: dict[str, Any], t0: datetime, t1: datetime) -> dict[str, Series]:
    """Ряды сигналов лифта за [t0, t1): значение на t0 (initial_value) и изменения внутри окна. Сигнал не найден: (None, [])."""
    plc_id = await pool.fetchval("SELECT id FROM plcs WHERE name = $1", cfg["plc"])
    if plc_id is None:
        raise HTTPException(status_code=404, detail=f"ПЛК {cfg['plc']} не найден")
    rows = await pool.fetch(
        "SELECT id, name FROM plc_tags WHERE plc_id = $1 AND name = ANY($2::text[]) AND upper(tag_type) = 'DIGITAL'",
        plc_id, cfg["signals"])
    ids = {r["name"]: r["id"] for r in rows}
    tag_ids = list(ids.values())
    series: dict[str, Series] = {n: (None, []) for n in cfg["signals"]}
    if not tag_ids:
        return series
    # значение на t0: последнее изменение до t0, первое изменение с t0, текущее значение (индекс tag_id, ts)
    init = await pool.fetch(
        """
        SELECT t.id,
               (SELECT value FROM digital_telemetry d WHERE d.tag_id = t.id AND d.ts < $2 ORDER BY d.ts DESC LIMIT 1) AS before,
               (SELECT value FROM digital_telemetry d WHERE d.tag_id = t.id AND d.ts >= $2 ORDER BY d.ts LIMIT 1) AS after,
               (SELECT value FROM digital_tag_state s WHERE s.tag_id = t.id LIMIT 1) AS cur
        FROM unnest($1::int[]) AS t(id)
        """,
        tag_ids, t0)
    init_by_id = {r["id"]: initial_value(r["before"], r["after"], r["cur"]) for r in init}
    ch = await pool.fetch(
        "SELECT tag_id, ts, value FROM digital_telemetry WHERE tag_id = ANY($1::int[]) AND ts >= $2 AND ts < $3 ORDER BY ts",
        tag_ids, t0, t1)
    by_id: dict[int, list[tuple[datetime, bool]]] = {i: [] for i in tag_ids}
    for r in ch:
        by_id[r["tag_id"]].append((r["ts"], bool(r["value"])))
    for name, i in ids.items():
        series[name] = (init_by_id.get(i), by_id[i])
    return series


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _out(c: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in c.items()}


def _threshold(threshold: float) -> float:
    if not (0 < threshold <= 3600):
        raise HTTPException(status_code=400, detail="Порог ожидания: от 0 до 3600 секунд")
    return threshold


@router.get("/{lift}/cycles")
async def cycles(request: Request, lift: str, date_from: date, date_to: date,
                 threshold: float = Query(DEFAULT_THRESHOLD_S), _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Циклы лифта за период (дни по времени завода): список без диаграмм и сводка; только вошедшим."""
    cfg = _cfg(lift)
    _threshold(threshold)
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="Дата «По» раньше даты «С»")
    if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=400, detail=f"Период не больше {MAX_PERIOD_DAYS} дней")
    t0, t1 = period_bounds(date_from, date_to)
    t1 = min(t1, max(t0, datetime.now(timezone.utc)))
    series = await load_series(request.app.state.pool, cfg, t0, t1)
    found = find_cycles(series, cfg, t0, t1, threshold)
    return {
        "lift": lift,
        "signals": cfg["signals"],
        "threshold": threshold,
        "from": t0.isoformat(),
        "to": t1.isoformat(),
        "summary": summary(found),
        "truncated": len(found) > MAX_CYCLES,
        "cycles": [_out(c) for c in found[:MAX_CYCLES]],
    }


@router.get("/{lift}/cycles/detail")
async def cycle_detail(request: Request, lift: str, start: datetime, threshold: float = Query(DEFAULT_THRESHOLD_S),
                       _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Один цикл (по точному времени начала из списка) и отрезки «= 1» каждого сигнала в окне диаграммы."""
    cfg = _cfg(lift)
    _threshold(threshold)
    if start.tzinfo is None:
        start = start.replace(tzinfo=_tz())
    t0 = start - timedelta(seconds=PRE_S)
    t1 = min(start + timedelta(seconds=DETAIL_HORIZON_S), max(start, datetime.now(timezone.utc)))
    series = await load_series(request.app.state.pool, cfg, t0, t1 + timedelta(seconds=POST_S))
    found = [c for c in find_cycles(series, cfg, t0, t1, threshold) if abs((c["start"] - start).total_seconds()) < 0.001]
    if not found:
        raise HTTPException(status_code=404, detail="Цикл с таким началом не найден")
    c = found[0]
    w0, w1 = view_window(c)
    return {
        "lift": lift,
        "signals": cfg["signals"],
        "threshold": threshold,
        "cycle": _out(c),
        "window": [w0.isoformat(), w1.isoformat()],
        "intervals": {n: [[a.isoformat(), b.isoformat()] for a, b in iv] for n, iv in intervals(series, cfg["signals"], c).items()},
    }
