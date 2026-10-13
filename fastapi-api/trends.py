"""Вкладка «Тренды» (технические графики): история связи с ПЛК и другие технические ряды."""
from __future__ import annotations

import io
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import asyncpg
import xlsxwriter
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from auth import require_admin

from period import _fmts, _xlsx_response

router = APIRouter(prefix="/api/trends")

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
MAX_DAYS = 93
STALE_SEC = 180   # статус связи старше этого: сборщик данных не работает, дальше связь неизвестна


async def ensure_trends_schema(pool: asyncpg.Pool) -> None:
    # События связи с ПЛК: запись только при смене состояния (потеря и восстановление связи), пишет сборщик данных (поллер)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS plc_link_events (
            id         BIGSERIAL PRIMARY KEY,
            plc_id     INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
            state      TEXT        NOT NULL,                 -- 'ok' связь есть, 'no_link' связи нет
            reason     TEXT        NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_plc_link_events_plc_time ON plc_link_events (plc_id, started_at DESC)")
    # Запись аналоговых переменных с мёртвой зоной: deadband (изменение, при котором пишем), limit_low и limit_high (вне них пишем каждый опрос)
    await pool.execute(
        """
        ALTER TABLE plc_tags
            ADD COLUMN IF NOT EXISTS deadband   DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS limit_low  DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS limit_high DOUBLE PRECISION
        """
    )
    # «Графики переменных» читают telemetry только по (tag_id, ts): отдельный индекс создаётся, лишь если подходящего ещё нет
    # (индекс idx_telemetry_tag_ts (tag_id, ts DESC) из init.sql подходит: диапазон читается в обе стороны)
    has = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'telemetry' AND indexdef ~ '\\(tag_id, ts( DESC)?\\)')")
    if not has:
        await pool.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_tag_ts ON telemetry (tag_id, ts)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tz() -> timezone:
    return timezone(timedelta(hours=UTC_OFFSET_HOURS))


def period_bounds(d1: date, d2: date) -> tuple[datetime, datetime]:
    """Границы периода по времени завода: с 00:00 первого дня до 00:00 дня после последнего."""
    tz = _tz()
    return datetime.combine(d1, time(), tz), datetime.combine(d2 + timedelta(days=1), time(), tz)


def link_segments(events: list[dict[str, Any]], ps: datetime, pe: datetime, now: datetime, status_at: datetime | None) -> list[dict[str, Any]]:
    """Единый расчёт отрезков связи одного ПЛК (его же используют экран и Excel).

    events: события по возрастанию времени {state, reason, started_at}. Отрезок идёт от события до следующего;
    состояние на начало периода задаёт последнее событие до периода (событий нет: 'unknown');
    расчёт идёт до min(конец периода, сейчас); если статус не обновлялся дольше STALE_SEC, после status_at состояние 'unknown'."""
    end = min(pe, now)
    if end <= ps:
        return []
    before = [e for e in events if e["started_at"] <= ps]
    inside = [e for e in events if ps < e["started_at"] < end]
    raw: list[list[Any]] = []
    if before:
        raw.append([before[-1]["state"], ps, before[-1]["reason"]])
    else:
        raw.append(["unknown", ps, ""])
    for e in inside:
        raw.append([e["state"], e["started_at"], e["reason"]])
    segs = [{"state": r[0], "start": r[1], "end": raw[i + 1][1] if i + 1 < len(raw) else end, "reason": r[2]} for i, r in enumerate(raw)]
    if status_at is not None and (now - status_at).total_seconds() > STALE_SEC and status_at < end:
        cut: list[dict[str, Any]] = []
        for sg in segs:
            if sg["end"] <= status_at:
                cut.append(sg)
            elif sg["start"] >= status_at:
                cut.append({**sg, "state": "unknown", "reason": ""})
            else:
                cut.append({**sg, "end": status_at})
                cut.append({"state": "unknown", "start": status_at, "end": sg["end"], "reason": ""})
        segs = cut
    merged: list[dict[str, Any]] = []
    for sg in segs:
        if sg["end"] <= sg["start"]:
            continue
        if merged and merged[-1]["state"] == sg["state"] and merged[-1]["reason"] == sg["reason"] and merged[-1]["end"] == sg["start"]:
            merged[-1]["end"] = sg["end"]
        else:
            merged.append(dict(sg))
    return merged


def link_summary(segs: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка: доля времени со связью (от известного времени), минуты без связи, число потерь связи (отрезков без связи)."""
    sec = {"ok": 0.0, "no_link": 0.0, "unknown": 0.0}
    for sg in segs:
        sec[sg["state"]] += (sg["end"] - sg["start"]).total_seconds()
    known = sec["ok"] + sec["no_link"]
    return {
        "pct_ok": (sec["ok"] / known) if known else None,
        "no_link_min": round(sec["no_link"] / 60, 1),
        "unknown_min": round(sec["unknown"] / 60, 1),
        "losses": sum(1 for sg in segs if sg["state"] == "no_link"),
    }


async def link_report(pool: asyncpg.Pool, d1: date, d2: date) -> dict[str, Any]:
    """Данные «Связь с ПЛК» за период по всем ПЛК в порядке списка (одна функция для экрана и Excel)."""
    ps, pe = period_bounds(d1, d2)
    now = _now()
    plcs = await pool.fetch("SELECT id, name, is_active FROM plcs ORDER BY sort_order NULLS LAST, id")
    status = {r["plc_id"]: r["updated_at"] for r in await pool.fetch("SELECT plc_id, updated_at FROM plc_connection_status")}
    ev_rows = await pool.fetch(
        "SELECT plc_id, state, reason, started_at FROM plc_link_events WHERE started_at < $1 ORDER BY plc_id, started_at, id", pe)
    events: dict[int, list[dict[str, Any]]] = {}
    for r in ev_rows:
        events.setdefault(r["plc_id"], []).append(dict(r))
    tz = _tz()
    out = []
    for p in plcs:
        segs = link_segments(events.get(p["id"], []), ps, pe, now, status.get(p["id"]))
        out.append({
            "plc_id": p["id"], "name": p["name"], "is_active": p["is_active"], "summary": link_summary(segs),
            "segments": [{"state": s["state"], "start": s["start"].astimezone(tz).isoformat(), "end": s["end"].astimezone(tz).isoformat(),
                          "minutes": round((s["end"] - s["start"]).total_seconds() / 60, 1), "reason": s["reason"]} for s in segs],
        })
    return {
        "from": d1.isoformat(), "to": d2.isoformat(), "period_start": ps.isoformat(), "period_end": pe.isoformat(),
        "now": now.astimezone(tz).isoformat(), "offset_hours": UTC_OFFSET_HOURS, "plcs": out,
        "totals": {"plcs_failed": sum(1 for p in out if p["summary"]["losses"] > 0),
                   "no_link_min": round(sum(p["summary"]["no_link_min"] for p in out), 1),
                   "losses": sum(p["summary"]["losses"] for p in out)},
    }


def _check_period(d1: date, d2: date) -> None:
    if d2 < d1:
        raise HTTPException(status_code=400, detail="Дата «по» раньше даты «с»")
    if (d2 - d1).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Период не больше {MAX_DAYS} дней")


def link_file_name(d1: date, d2: date) -> str:
    return f"связь_{d1.isoformat()}.xlsx" if d1 == d2 else f"связь_{d1.isoformat()}_{d2.isoformat()}.xlsx"


def build_link_workbook(rep: dict[str, Any]) -> bytes:
    """Excel как экран: заголовок, плитки итогов, сводка по ПЛК, список потерь связи (суммы формулами SUM)."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    dt_f = wb.add_format({"num_format": "dd.mm.yyyy hh:mm", "align": "left"})
    ws = wb.add_worksheet("Связь")
    d1, d2 = date.fromisoformat(rep["from"]), date.fromisoformat(rep["to"])
    span = d1.strftime("%d.%m.%Y") if d1 == d2 else f"{d1.strftime('%d.%m.%Y')} — {d2.strftime('%d.%m.%Y')}"
    ws.merge_range(0, 0, 0, 4, f"Связь с ПЛК, период {span}", f["title"])
    plcs, tot = rep["plcs"], rep["totals"]
    tiles = [("ПЛК со сбоями", tot["plcs_failed"], "tile_v"), ("Минут без связи", tot["no_link_min"], "tile_bad"), ("Потерь связи", tot["losses"], "tile_v")]
    for c, (lab, val, fm) in enumerate(tiles):
        ws.write(2, c, lab, f["tile_l"])
        ws.write(3, c, val, f[fm])
    ws.set_row(3, 30)
    ws.write(5, 0, "По ПЛК", f["section"])
    ws.write_row(6, 0, ["ПЛК", "В списке", "Со связью, %", "Минут без связи", "Потерь связи"], f["head"])
    for i, p in enumerate(plcs):
        r, s = 7 + i, p["summary"]
        ws.write(r, 0, p["name"])
        ws.write(r, 1, "включён" if p["is_active"] else "выключен")
        if s["pct_ok"] is None:
            ws.write(r, 2, "—")
        else:
            ws.write(r, 2, s["pct_ok"], f["pct"])
        ws.write(r, 3, s["no_link_min"], f["n1"])
        ws.write(r, 4, s["losses"])
    r_tot = 7 + len(plcs)
    ws.write(r_tot, 0, "Итого", f["bold"])
    ws.write_formula(r_tot, 3, f"=SUM(D8:D{r_tot})" if plcs else "=0", f["bn1"], tot["no_link_min"])
    ws.write_formula(r_tot, 4, f"=SUM(E8:E{r_tot})" if plcs else "=0", f["bold"], tot["losses"])
    top = r_tot + 3
    ws.write(top - 1, 0, "Потери связи", f["section"])
    ws.write_row(top, 0, ["ПЛК", "Начало", "Конец", "Минут", "Причина"], f["head"])
    rows = [(p["name"], sg) for p in plcs for sg in p["segments"] if sg["state"] == "no_link"]
    for i, (name, sg) in enumerate(rows):
        r = top + 1 + i
        ws.write(r, 0, name)
        ws.write_datetime(r, 1, datetime.fromisoformat(sg["start"]).replace(tzinfo=None), dt_f)
        ws.write_datetime(r, 2, datetime.fromisoformat(sg["end"]).replace(tzinfo=None), dt_f)
        ws.write(r, 3, sg["minutes"], f["n1"])
        ws.write(r, 4, sg["reason"])
    last = top + 1 + len(rows)
    ws.write(last, 0, "Итого", f["bold"])
    ws.write_formula(last, 3, f"=SUM(D{top + 2}:D{last})" if rows else "=0", f["bn1"], round(sum(sg["minutes"] for _, sg in rows), 1))
    for c, w in enumerate([26, 20, 20, 16, 40]):
        ws.set_column(c, c, w)
    if plcs:
        # диаграмма «Связь с ПЛК»: по ПЛК минуты со связью, без связи и без данных (цвета как у лент на экране); данные на скрытом листе
        cs = wb.add_worksheet("График")
        cs.write_row(0, 0, ["ПЛК", "Связь есть", "Связи нет", "Нет данных"], f["head"])
        for i, p in enumerate(plcs, start=1):
            mins = {"ok": 0.0, "no_link": 0.0, "unknown": 0.0}
            for sg in p["segments"]:
                mins[sg["state"]] += sg["minutes"]
            cs.write(i, 0, p["name"])
            cs.write_row(i, 1, [round(mins["ok"], 1), round(mins["no_link"], 1), round(mins["unknown"], 1)], f["n1"])
        cs.hide()
        ch = wb.add_chart({"type": "bar", "subtype": "stacked"})
        for col, name, color in ((1, "Связь есть", "#27AE60"), (2, "Связи нет", "#E88B8B"), (3, "Нет данных", "#B8C0CC")):
            ch.add_series({"name": name, "categories": ["График", 1, 0, len(plcs), 0], "values": ["График", 1, col, len(plcs), col],
                           "fill": {"color": color}, "gap": 40})
        ch.set_title({"name": "Связь с ПЛК"})
        ch.set_y_axis({"reverse": True})
        ch.set_x_axis({"name": "Минут", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
        ch.set_legend({"position": "bottom"})
        ch.set_size({"width": 640, "height": max(200, 60 + 30 * len(plcs))})
        ws.insert_chart(2, 6, ch)
    wb.close()
    return buf.getvalue()


@router.get("/link")
async def link(request: Request, date_from: date = Query(alias="from"), date_to: date = Query(alias="to")) -> dict[str, Any]:
    """Связь с ПЛК за период (отрезки и сводка по каждому ПЛК); доступна всем без входа."""
    _check_period(date_from, date_to)
    return await link_report(request.app.state.pool, date_from, date_to)


@router.get("/link.xlsx")
async def link_xlsx(request: Request, date_from: date = Query(alias="from"), date_to: date = Query(alias="to"), src: str = Query(default="trends")) -> Response:
    """Те же данные в Excel: Тренды_связь_<с>_<по>.xlsx; доступна всем без входа."""
    _check_period(date_from, date_to)
    rep = await link_report(request.app.state.pool, date_from, date_to)
    return _xlsx_response(build_link_workbook(rep), link_file_name(date_from, date_to), src)


# ------------------------------------------------------------------ запись аналоговых переменных (мёртвая зона), только администратор
class TagRecordBody(BaseModel):
    deadband: float | None = None
    limit_low: float | None = None
    limit_high: float | None = None


def validate_record_settings(deadband: float | None, limit_low: float | None, limit_high: float | None) -> None:
    """Проверка настроек записи: мёртвая зона не отрицательная, пороги конечные, нижний меньше верхнего."""
    for label, v in (("Мёртвая зона", deadband), ("Нижний порог", limit_low), ("Верхний порог", limit_high)):
        if v is not None and not math.isfinite(v):
            raise HTTPException(status_code=422, detail=f"{label}: нужно конечное число")
    if deadband is not None and deadband < 0:
        raise HTTPException(status_code=422, detail="Мёртвая зона не может быть отрицательной")
    if limit_low is not None and limit_high is not None and limit_low >= limit_high:
        raise HTTPException(status_code=422, detail="Нижний порог должен быть меньше верхнего")


_TAG_SELECT = (
    "SELECT t.id AS tag_id, t.plc_id, p.name AS plc_name, t.name, t.node_id, t.deadband, t.limit_low, t.limit_high "
    "FROM plc_tags t JOIN plcs p ON p.id = t.plc_id "
)


@router.get("/tags")
async def record_tags(request: Request, _: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    """Архивные аналоговые переменные с настройками записи (ПЛК в порядке списка); только администратор."""
    rows = await request.app.state.pool.fetch(
        _TAG_SELECT + "WHERE t.is_archived AND upper(t.tag_type) = 'ANALOG' ORDER BY p.sort_order NULLS LAST, p.id, t.name, t.id")
    return [dict(r) for r in rows]


@router.put("/tags/{tag_id}")
async def update_record_tag(tag_id: int, body: TagRecordBody, request: Request, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Сохранить мёртвую зону и пороги переменной (пустое значение снимает настройку); только администратор."""
    validate_record_settings(body.deadband, body.limit_low, body.limit_high)
    pool = request.app.state.pool
    ok = await pool.fetchval(
        "UPDATE plc_tags SET deadband = $2, limit_low = $3, limit_high = $4 WHERE id = $1 RETURNING id",
        tag_id, body.deadband, body.limit_low, body.limit_high)
    if ok is None:
        raise HTTPException(status_code=404, detail="Переменная не найдена")
    return dict(await pool.fetchrow(_TAG_SELECT + "WHERE t.id = $1", tag_id))


# ------------------------------------------------------------------ «Графики переменных»: ряды записанных переменных (telemetry)
MAX_TAGS = 8            # переменных на один график
RAW_LIMIT = 2000        # до стольких точек на переменную отдаются сырые данные, дальше корзины по времени
MIN_BUCKETS, MAX_BUCKETS, DEFAULT_BUCKETS = 50, 2000, 1000
XL_BUCKETS = 1500       # точек на ряд в Excel
GAP_SEC = 10            # пропуск между секундными записями больше этого рисуется разрывом линии
# цвета линий (светлая тема, как на экране: --chart-1…8 в downtime.css); в Excel те же
CHART_COLORS = ["#2b6cb0", "#7c3aed", "#0e7490", "#be185d", "#1d2733", "#a16207", "#4f46e5", "#64748b"]


def parse_tag_ids(raw: str) -> list[int]:
    """Список id переменных из «8,9,10»: без повторов, от 1 до MAX_TAGS, иначе 422."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        raise HTTPException(status_code=422, detail="Не выбрано ни одной переменной")
    ids: list[int] = []
    for p in parts:
        if not re.fullmatch(r"\d+", p) or int(p) <= 0:
            raise HTTPException(status_code=422, detail=f"Неверный номер переменной: {p}")
        if int(p) not in ids:
            ids.append(int(p))
    if len(ids) > MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"Не больше {MAX_TAGS} переменных на график")
    return ids


def check_window(f: datetime, t: datetime) -> tuple[datetime, datetime]:
    """Окно графика: время без пояса считается временем завода; конец позже начала, не длиннее MAX_DAYS суток."""
    f = f if f.tzinfo else f.replace(tzinfo=_tz())
    t = t if t.tzinfo else t.replace(tzinfo=_tz())
    if t <= f:
        raise HTTPException(status_code=422, detail="Конец периода должен быть позже начала")
    if t - f > timedelta(days=MAX_DAYS):
        raise HTTPException(status_code=422, detail=f"Период не больше {MAX_DAYS} дней")
    return f, t


def bucket_width_sec(span_sec: float, buckets: int) -> int:
    """Ширина корзины в секундах (целая, не меньше секунды): корзин получается не больше заказанного."""
    return max(1, math.ceil(span_sec / max(1, buckets)))


def use_raw(count: int) -> bool:
    return count <= RAW_LIMIT


def is_step(deadband: float | None) -> bool:
    """Переменные с мёртвой зоной пишутся по изменению: значение держится до следующей записи, рисуются ступенькой."""
    return deadband is not None and deadband > 0


def stats_from_raw(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"min": None, "avg": None, "max": None, "count": 0}
    return {"min": min(vals), "avg": sum(vals) / len(vals), "max": max(vals), "count": len(vals)}


def stats_from_buckets(rows: list[tuple[float, float, float, int]]) -> dict[str, Any]:
    """Итоги по корзинам (min, avg, max, n): среднее взвешено числом точек, поэтому равно среднему по сырым данным."""
    n = sum(r[3] for r in rows)
    if not n:
        return {"min": None, "avg": None, "max": None, "count": 0}
    return {"min": min(r[0] for r in rows), "avg": sum(r[1] * r[3] for r in rows) / n, "max": max(r[2] for r in rows), "count": n}


def _ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


async def fetch_series(pool: asyncpg.Pool, ids: list[int], f: datetime, t: datetime, buckets: int, force_agg: bool = False) -> dict[str, Any]:
    """Ряды выбранных переменных за окно (одна функция для экрана и Excel).

    Переменная с числом точек до RAW_LIMIT отдаётся сырыми [мс, значение]; остальные корзинами по времени
    [начало корзины мс, min, avg, max, n] (пустые корзины не возвращаются); force_agg: всегда корзины (Excel).
    Для ступенчатых добавляется prior: последняя запись до окна."""
    meta_rows = await pool.fetch(
        "SELECT t.id AS tag_id, t.plc_id, p.name AS plc_name, t.name, t.node_id, t.deadband, t.limit_low, t.limit_high "
        "FROM plc_tags t JOIN plcs p ON p.id = t.plc_id WHERE t.id = ANY($1::int[])", ids)
    meta = {r["tag_id"]: dict(r) for r in meta_rows}
    missing = [i for i in ids if i not in meta]
    if missing:
        raise HTTPException(status_code=422, detail=f"Переменная не найдена: {missing[0]}")
    span = (t - f).total_seconds()
    w = bucket_width_sec(span, buckets)
    if force_agg:
        raw_ids: list[int] = []
    else:
        cnt = await pool.fetch(
            "SELECT s.id, (SELECT count(*) FROM (SELECT 1 FROM telemetry m WHERE m.tag_id = s.id AND m.ts >= $2 AND m.ts <= $3 LIMIT $4) q) AS n "
            "FROM unnest($1::int[]) s(id)", ids, f, t, RAW_LIMIT + 1)
        over = {r["id"] for r in cnt if not use_raw(r["n"])}
        raw_ids = [i for i in ids if i not in over]
    agg_ids = [i for i in ids if i not in raw_ids]
    raw: dict[int, list[list[Any]]] = {i: [] for i in raw_ids}
    if raw_ids:
        for r in await pool.fetch(
                "SELECT tag_id, ts, value FROM telemetry WHERE tag_id = ANY($1::int[]) AND ts >= $2 AND ts <= $3 ORDER BY tag_id, ts", raw_ids, f, t):
            raw[r["tag_id"]].append([_ms(r["ts"]), r["value"]])
    agg: dict[int, list[list[Any]]] = {i: [] for i in agg_ids}
    f_ms = _ms(f)
    if agg_ids:
        for r in await pool.fetch(
                "SELECT tag_id, floor((extract(epoch from ts)::float8 - $4::float8) / $5::float8)::int AS b, "
                "min(value) AS mn, avg(value) AS av, max(value) AS mx, count(*) AS n "
                "FROM telemetry WHERE tag_id = ANY($1::int[]) AND ts >= $2 AND ts <= $3 GROUP BY tag_id, b ORDER BY tag_id, b",
                agg_ids, f, t, f.timestamp(), float(w)):
            agg[r["tag_id"]].append([f_ms + r["b"] * w * 1000, r["mn"], r["av"], r["mx"], r["n"]])
    step_ids = [i for i in ids if is_step(meta[i]["deadband"])]
    prior: dict[int, list[Any]] = {}
    if step_ids:
        for r in await pool.fetch(
                "SELECT s.id, p.ts, p.value FROM unnest($1::int[]) s(id) CROSS JOIN LATERAL "
                "(SELECT m.ts, m.value FROM telemetry m WHERE m.tag_id = s.id AND m.ts < $2 ORDER BY m.ts DESC LIMIT 1) p", step_ids, f):
            prior[r["id"]] = [_ms(r["ts"]), r["value"]]
    out = []
    for i in ids:
        m = meta[i]
        if i in raw:
            pts, st, mode = raw[i], stats_from_raw([p[1] for p in raw[i]]), "raw"
        else:
            pts, st, mode = agg[i], stats_from_buckets([(p[1], p[2], p[3], p[4]) for p in agg[i]]), "agg"
        out.append({**m, "step": is_step(m["deadband"]), "gap_sec": GAP_SEC, "mode": mode, "points": pts, "prior": prior.get(i), "stats": st})
    tz = _tz()
    return {"from": f.astimezone(tz).isoformat(), "to": t.astimezone(tz).isoformat(), "from_ms": f_ms, "to_ms": _ms(t),
            "now_ms": _ms(_now()), "offset_hours": UTC_OFFSET_HOURS, "bucket_sec": w, "series": out}


@router.get("/vars")
async def graph_vars(request: Request) -> dict[str, Any]:
    """Записанные аналоговые переменные (отмечены «Архив») по ПЛК в порядке списка, с мёртвой зоной и границами; доступна всем без входа."""
    pool = request.app.state.pool
    plcs = await pool.fetch("SELECT id, name, is_active FROM plcs ORDER BY sort_order NULLS LAST, id")
    rows = await pool.fetch(
        "SELECT id AS tag_id, plc_id, name, node_id, deadband, limit_low, limit_high FROM plc_tags "
        "WHERE is_archived AND upper(tag_type) = 'ANALOG' ORDER BY name, id")
    by: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["plc_id"], []).append(dict(r))
    return {"offset_hours": UTC_OFFSET_HOURS,
            "plcs": [{"plc_id": p["id"], "name": p["name"], "is_active": p["is_active"], "tags": by[p["id"]]} for p in plcs if p["id"] in by]}


@router.get("/series")
async def series(request: Request, date_from: datetime = Query(alias="from"), date_to: datetime = Query(alias="to"),
                 tags: str = Query(), buckets: int = Query(default=DEFAULT_BUCKETS, ge=MIN_BUCKETS, le=MAX_BUCKETS)) -> dict[str, Any]:
    """Ряды выбранных переменных за окно (сырые точки или min/avg/max по корзинам времени); доступна всем без входа."""
    f, t = check_window(date_from, date_to)
    return await fetch_series(request.app.state.pool, parse_tag_ids(tags), f, t, buckets)


def series_file_name(plc_name: str, f: datetime, t: datetime) -> str:
    """<ПЛК>_<период>.xlsx (перед ним экран «Тренды_»): целые сутки дают даты, часть суток даёт дату и время ЧЧ-ММ."""
    tz = _tz()
    a, b = f.astimezone(tz), t.astimezone(tz)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", plc_name).strip("_") or "ПЛК"
    midnight = a.time() == time() and b.time() == time()
    last = (b - timedelta(seconds=1)).date()
    if midnight:
        span = a.date().isoformat() if last == a.date() else f"{a.date().isoformat()}_{last.isoformat()}"
    elif a.date() == b.date():
        span = f"{a.date().isoformat()}_{a:%H-%M}_{b:%H-%M}"
    else:
        span = f"{a.date().isoformat()}_{a:%H-%M}_{b.date().isoformat()}_{b:%H-%M}"
    return f"{safe}_{span}.xlsx"


def _plain_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, _tz()).replace(tzinfo=None)


def build_series_workbook(rep: dict[str, Any], plc_name: str) -> bytes:
    """Excel как экран «Графики переменных»: заголовок, график (линии по среднему в корзине), таблица «Статистика за видимый период»;
    данные графика на скрытом листе «График» (общая сетка времени; пропуск пустой ячейкой, ступенчатые продолжаются до следующей записи)."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    num = wb.add_format({"num_format": "0.00"})
    dt_f = wb.add_format({"num_format": "dd.mm.yyyy hh:mm:ss", "align": "left"})
    ws = wb.add_worksheet("Графики переменных")
    cs = wb.add_worksheet("График")
    sr = rep["series"]
    a, b = _plain_dt(rep["from_ms"]), _plain_dt(rep["to_ms"])
    span = f"{a:%d.%m.%Y %H:%M} — {b:%d.%m.%Y %H:%M}"
    ws.merge_range(0, 0, 0, 5, f"Графики переменных: {plc_name}, период {span}", f["title"])
    # данные графика: строка на корзину общей сетки
    w_ms = rep["bucket_sec"] * 1000
    n_rows = (rep["to_ms"] - rep["from_ms"]) // w_ms + 1
    cs.write(0, 0, "Время", f["head"])
    for c, s in enumerate(sr, start=1):
        cs.write(0, c, s["name"], f["head"])
    for k in range(n_rows):
        cs.write_datetime(1 + k, 0, _plain_dt(rep["from_ms"] + k * w_ms), dt_f)
    for c, s in enumerate(sr, start=1):
        by = {p[0]: p[2] for p in s["points"]}
        held = s["prior"][1] if s["step"] and s["prior"] else None
        for k in range(n_rows):
            v = by.get(rep["from_ms"] + k * w_ms)
            if v is None and s["step"]:
                v = held
            if v is not None:
                held = v if s["step"] else None
                cs.write_number(1 + k, c, v, num)
    cs.set_column(0, 0, 20)
    cs.hide()
    chart_rows = 20
    ws.write(2, 0, "Графики переменных", f["section"])
    ch = wb.add_chart({"type": "line"})
    for c, s in enumerate(sr, start=1):
        ch.add_series({"name": s["name"], "categories": ["График", 1, 0, n_rows, 0], "values": ["График", 1, c, n_rows, c],
                       "line": {"color": CHART_COLORS[(c - 1) % len(CHART_COLORS)], "width": 1.25}, "marker": {"type": "none"}})
    ch.set_title({"name": "Графики переменных"})
    ch.set_x_axis({"date_axis": True, "num_format": "dd.mm hh:mm", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
    ch.set_y_axis({"name": "Значение", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
    ch.set_legend({"position": "bottom"})
    ch.show_blanks_as("gap")
    ch.set_size({"width": 900, "height": 380})
    ws.insert_chart(3, 0, ch)
    top = 3 + chart_rows + 1
    ws.write(top, 0, "Статистика за видимый период", f["section"])
    ws.write_row(top + 1, 0, ["Переменная", "Минимум", "Среднее", "Максимум", "Точек"], f["head"])
    for i, s in enumerate(sr):
        st = s["stats"]
        r = top + 2 + i
        ws.write(r, 0, s["name"])
        for c, key in ((1, "min"), (2, "avg"), (3, "max")):
            if st[key] is None:
                ws.write(r, c, "—")
            else:
                ws.write_number(r, c, st[key], num)
        ws.write_number(r, 4, st["count"])
    for c, wd in enumerate([46, 14, 14, 14, 12]):
        ws.set_column(c, c, wd)
    wb.close()
    return buf.getvalue()


@router.get("/series.xlsx")
async def series_xlsx(request: Request, date_from: datetime = Query(alias="from"), date_to: datetime = Query(alias="to"),
                      tags: str = Query(), plc: str = Query(default=""), src: str = Query(default="trends")) -> Response:
    """Те же ряды в Excel: Тренды_<ПЛК>_<период>.xlsx (график по корзинам, до XL_BUCKETS точек на ряд); доступна всем без входа."""
    f, t = check_window(date_from, date_to)
    rep = await fetch_series(request.app.state.pool, parse_tag_ids(tags), f, t, XL_BUCKETS, force_agg=True)
    name = plc.strip() or (rep["series"][0]["plc_name"] if rep["series"] else "ПЛК")
    return _xlsx_response(build_series_workbook(rep, name), series_file_name(name, f, t), src)
