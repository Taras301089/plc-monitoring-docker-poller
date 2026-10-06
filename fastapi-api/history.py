"""История экрана Andon по дням: почасовая таблица за выбранный день и отчёт дня в Excel (.xlsx) с графиком план/факт."""
from __future__ import annotations

import io
import re
from datetime import date
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import asyncpg
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from downtime import _screen_path, list_downtimes

router = APIRouter(prefix="/api/kpi")

DAY_HEADERS = ["Дата", "Линия", "№", "Начало", "Окончание", "Мин.", "План", "Факт", "±", "Такт-тайм за период",
               "Простой мин", "Участок", "Станция", "Причина", "Мин. по причине", "Описание", "Внёс", "Последний комментарий"]
DAY_WIDTHS = [11, 22, 5, 9, 10, 6, 7, 7, 6, 14, 11, 16, 16, 20, 14, 36, 24, 44]


def _hhmm(m: int | None) -> str:
    if m is None:
        return ""
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _mmss(sec: int | None) -> str:
    if not sec or sec <= 0:
        return "—"
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_name(s: str) -> str:
    """Имя для файла и папки: без символов, запрещённых в Windows."""
    return re.sub(r'[\\/:*?"<>|]+', "-", s).strip(" .") or "line"


async def _day_rows(pool: asyncpg.Pool, screen_id: int, day: date) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT idx, start_min, end_min, plan, fact, takt_sec FROM kpi_hourly WHERE screen_id = $1 AND prod_date = $2 ORDER BY idx",
        screen_id, day,
    )


@router.get("/screens/{screen_id}/hourly")
async def hourly(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date")) -> dict[str, Any]:
    """Почасовые интервалы экрана за день (план, факт, такт) и список дней, за которые есть данные."""
    pool: asyncpg.Pool = request.app.state.pool
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    dates = [r["d"].isoformat() for r in await pool.fetch(
        "SELECT DISTINCT prod_date AS d FROM kpi_hourly WHERE screen_id = $1 ORDER BY d DESC LIMIT 400", screen_id)]
    if day is None:
        return {"date": None, "dates": dates, "intervals": []}
    rows = await _day_rows(pool, screen_id, day)
    return {
        "date": day.isoformat(), "dates": dates,
        "intervals": [{"idx": r["idx"], "start_min": r["start_min"], "end_min": r["end_min"], "plan": r["plan"],
                       "fact": r["fact"], "takt_sec": r["takt_sec"]} for r in rows],
    }


def build_day_workbook(line: str, day: date, rows: list[list[Any]], intervals: list[tuple[str, int, int]]) -> bytes:
    """Книга Excel: лист «День» (таблица с фильтром) и лист «План-факт» (почасовая таблица и столбчатая диаграмма)."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"})

    ws = wb.add_worksheet("День")
    ws.write_row(0, 0, DAY_HEADERS, head)
    for r, row in enumerate(rows, start=1):
        ws.write_row(r, 0, row)
    for c, w in enumerate(DAY_WIDTHS):
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(rows), 1), len(DAY_HEADERS) - 1)

    cs = wb.add_worksheet("План-факт")
    cs.write_row(0, 0, ["Интервал", "План", "Факт", "±"], head)
    for r, (label, plan, fact) in enumerate(intervals, start=1):
        cs.write(r, 0, label)
        cs.write(r, 1, plan)
        cs.write(r, 2, fact)
        cs.write_formula(r, 3, f"=C{r + 1}-B{r + 1}", None, fact - plan)
    cs.set_column(0, 0, 14)
    cs.set_column(1, 3, 9)
    n = len(intervals)
    if n:
        chart = wb.add_chart({"type": "column"})
        for col, name, color in ((1, "План", "#8FA3BF"), (2, "Факт", "#2F80ED")):
            chart.add_series({
                "name": name,
                "categories": ["План-факт", 1, 0, n, 0],
                "values": ["План-факт", 1, col, n, col],
                "fill": {"color": color},
                "data_labels": {"value": True},
                "gap": 80,
            })
        chart.set_title({"name": f"План и факт по часам: {line}, {day.strftime('%d.%m.%Y')}"})
        chart.set_x_axis({"name": "Интервал"})
        chart.set_y_axis({"name": "Кузовов", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 860, "height": 400})
        cs.insert_chart("F2", chart)
    wb.close()
    return buf.getvalue()


async def build_day_report(pool: asyncpg.Pool, screen_id: int, day: date) -> tuple[bytes, str, str, int] | None:
    """Отчёт дня по экрану: (книга, имя папки линии, имя файла, число интервалов с планом или фактом); None, если экрана нет."""
    scr = await pool.fetchrow("SELECT name, template, bindings FROM kpi_screens WHERE id = $1", screen_id)
    if scr is None:
        return None
    # как на экране: показываются только интервалы с планом или фактом
    hours = [h for h in await _day_rows(pool, screen_id, day) if h["plan"] > 0 or h["fact"] > 0]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    dts = {d["idx"]: d for d in (await list_downtimes(screen_id, req, day))["downtimes"]}
    areas = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_areas")}
    stations = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_stations")}
    reasons = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_reasons")}
    group, label = _screen_path(scr["template"], scr["name"], scr["bindings"])
    line = f"{group} › {label}"

    out: list[list[Any]] = []
    for h in hours:
        dur = (h["end_min"] or h["start_min"]) - h["start_min"]
        delta = h["fact"] - h["plan"]
        base = [day.strftime("%d.%m.%Y"), line, h["idx"], _hhmm(h["start_min"]), _hhmm(h["end_min"]), dur, h["plan"], h["fact"], delta,
                _mmss(h["takt_sec"])]
        d = dts.get(h["idx"])
        items = (d or {}).get("items") or []
        if not d or (d["minutes"] <= 0 and not items):
            out.append(base + [0, "—", "—", "—", 0, "", "", ""])
            continue
        for it in items:
            lc = it.get("last_comment")
            out.append(base + [d["minutes"], areas.get(it["area_id"], "—"), stations.get(it["station_id"], "—"),
                               reasons.get(it["reason_id"], "—"), it["minutes"], it.get("note") or "", it.get("created_by_name") or "",
                               f"{lc['user_name']}: {lc['text']}" if lc else ""])
        rest = round(d["minutes"] - d["described"], 1)
        if rest > 0.5:
            out.append(base + [d["minutes"], "—", "—", "не описан", rest, "", "", ""])
    intervals = [(f"{_hhmm(h['start_min'])}-{_hhmm(h['end_min'] if h['end_min'] is not None else h['start_min'])}", h["plan"], h["fact"])
                 for h in hours]
    data = build_day_workbook(line, day, out, intervals)
    folder = safe_name(group if label in ("", "Main Line") else f"{group}_{label}")
    return data, folder, f"{folder}_{day.isoformat()}.xlsx", len(hours)


@router.get("/screens/{screen_id}/day.xlsx")
async def day_xlsx(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date")) -> Response:
    """Выгрузка дня экрана в Excel: по строке на каждую причину простоя, интервалы без простоя одной строкой, лист с графиком план/факт."""
    pool: asyncpg.Pool = request.app.state.pool
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    if day is None:
        day = await pool.fetchval("SELECT max(prod_date) FROM kpi_hourly WHERE screen_id = $1", screen_id)
        if day is None:
            raise HTTPException(status_code=404, detail="По этому экрану ещё нет данных")
    data, folder, fname, _ = await build_day_report(pool, screen_id, day)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('andon_' + fname)}"},
    )
