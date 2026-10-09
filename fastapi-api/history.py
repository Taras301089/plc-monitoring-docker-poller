"""История экрана Andon по дням: почасовая таблица за выбранный день и отчёт дня в Excel (.xlsx): один лист с итогами и графиком план/факт."""
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
# имя файла начинается с названия экрана, откуда его скачали (правило в CLAUDE.md)
SRC_NAMES = {"andon": "Andon", "charts": "Графики", "reports": "Отчёты", "downtimes": "Простои", "trends": "Тренды"}


def download_name(src: str, fname: str) -> str:
    return f"{SRC_NAMES.get(src, SRC_NAMES['reports'])}_{fname}"


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
    """Книга Excel по виду как вкладка «Отчёты»: на листе «День» сверху плитки итогов, под ними график план/факт по часам,
    ниже таблица отчёта (с фильтром и итогами). Данные для графика лежат на скрытом листе «График»."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"})
    bold = wb.add_format({"bold": True, "top": 1})
    bold_n1 = wb.add_format({"bold": True, "top": 1, "num_format": "0.0"})
    bold_pct = wb.add_format({"bold": True, "num_format": "0.0%"})
    title = wb.add_format({"bold": True, "font_size": 14})
    tile = {"font_size": 18, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}
    tile_l = wb.add_format({"font_size": 9, "bold": True, "font_color": "#6B7785", "bg_color": "#F2F4F7", "align": "center", "valign": "vcenter",
                            "border": 1, "border_color": "#D9D9D9"})
    tile_f = {"plain": wb.add_format(tile), "ok": wb.add_format({**tile, "font_color": "#1E8E5A"}),
              "pct": wb.add_format({**tile, "num_format": "0.0%"}), "bad": wb.add_format({**tile, "num_format": "0.0", "font_color": "#C0392B"})}

    ws = wb.add_worksheet("День")
    for c, w in enumerate(DAY_WIDTHS):
        ws.set_column(c, c, w)
    n_rows, n = len(rows), len(intervals)

    def total(col: int) -> float:
        return round(sum(float(r[col] or 0) for r in rows), 1)

    # 1. плитки итогов (как на экране)
    ws.write(0, 0, f"{line}, {day.strftime('%d.%m.%Y')}", title)
    tp, tf = total(6), total(7)
    seen: set[Any] = set()
    down = 0.0
    for r in rows:
        if r[2] not in seen:
            seen.add(r[2])
            down += float(r[10] or 0)
    tiles = [((0, 1), "План", tp, "plain"), ((2, 4), "Факт", tf, "ok"), ((5, 7), "Выполнение", (tf / tp) if tp else "", "pct"),
             ((8, 10), "Простой, мин", round(down, 1), "bad")]
    for (c1, c2), label, val, kind in tiles:
        ws.merge_range(2, c1, 2, c2, label, tile_l)
        ws.merge_range(3, c1, 3, c2, val, tile_f[kind])
    ws.set_row(2, 18)
    ws.set_row(3, 32)

    # 2. график план/факт по часам, под плитками
    chart_row = 5
    table_row = chart_row + (-(-400 // 20) + 2 if n else 0)

    # 3. таблица отчёта: шапка с фильтром, строки, итоги
    ws.write_row(table_row, 0, DAY_HEADERS, head)
    for r, row in enumerate(rows, start=table_row + 1):
        ws.write_row(r, 0, row)
    ws.autofilter(table_row, 0, table_row + max(n_rows, 1), len(DAY_HEADERS) - 1)
    if n_rows:
        # значения интервала записаны один раз, поэтому суммы столбцов верные
        t = table_row + n_rows + 1
        first, last = table_row + 2, table_row + n_rows + 1
        ws.write(t, 0, "Итого", bold)
        for c in range(1, len(DAY_HEADERS)):
            ws.write_blank(t, c, None, bold)
        for col in (5, 6, 7, 10, 14):
            letter = xlsxwriter.utility.xl_col_to_name(col)
            ws.write_formula(t, col, f"=SUM({letter}{first}:{letter}{last})", bold_n1 if col in (10, 14) else bold, total(col))
        ws.write_formula(t, 8, f"=H{t + 1}-G{t + 1}", bold, tf - tp)
        ws.write(t + 1, 0, "Выполнение", bold_pct)
        ws.write_formula(t + 1, 7, f'=IF(G{t + 1}=0,"",H{t + 1}/G{t + 1})', bold_pct, (tf / tp) if tp else "")

    if n:
        cs = wb.add_worksheet("График")
        cs.write_row(0, 0, ["Интервал", "План", "Факт", "% выполнения"], head)
        for r, (label, plan, fact) in enumerate(intervals, start=1):
            cs.write(r, 0, label)
            cs.write(r, 1, plan)
            cs.write(r, 2, fact)
            if plan:   # без плана процента нет (на экране точки нет)
                cs.write_formula(r, 3, f"=C{r + 1}/B{r + 1}", None, fact / plan)
        cs.hide()
        chart = wb.add_chart({"type": "column"})
        for col, name, color in ((1, "План", "#8FA3BF"), (2, "Факт", "#2F80ED")):
            chart.add_series({
                "name": name,
                "categories": ["График", 1, 0, n, 0],
                "values": ["График", 1, col, n, col],
                "fill": {"color": color},
                "data_labels": {"value": True},
                "gap": 80,
            })
        ln = wb.add_chart({"type": "line"})
        ln.add_series({"name": "% выполнения", "categories": ["График", 1, 0, n, 0], "values": ["График", 1, 3, n, 3], "y2_axis": True,
                       "line": {"color": "#27AE60", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
        chart.combine(ln)
        ln.set_y2_axis({"name": "% выполнения", "num_format": "0%"})
        chart.set_title({"name": "План и факт по часам"})
        chart.set_x_axis({"name": "Интервал"})
        chart.set_y_axis({"name": "Кузовов", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 1000, "height": 400})
        ws.insert_chart(chart_row, 0, chart)
    wb.close()
    return buf.getvalue()


async def cumulative_day(pool: asyncpg.Pool, screen_id: int, day: date) -> list[dict[str, Any]]:
    """Накопленный за день план и факт по интервалам (как таблица Andon: только интервалы с планом или фактом).
    Строка: интервал, план, факт, накопленные план и факт, разница накопленных (факт минус план). Последние накопленные = «Итого» дня."""
    out: list[dict[str, Any]] = []
    cp = cf = 0
    for h in await _day_rows(pool, screen_id, day):
        if not (h["plan"] > 0 or h["fact"] > 0):
            continue
        cp += h["plan"]
        cf += h["fact"]
        end = h["end_min"] if h["end_min"] is not None else h["start_min"]
        out.append({"idx": h["idx"], "label": f"{_hhmm(h['start_min'])}-{_hhmm(end)}", "plan": h["plan"], "fact": h["fact"],
                    "cum_plan": cp, "cum_fact": cf, "diff": cf - cp})
    return out


async def build_day_data(pool: asyncpg.Pool, screen_id: int, day: date) -> dict[str, Any] | None:
    """Данные отчёта дня: подпись линии, строки таблицы, интервалы для графика; None, если экрана нет."""
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
        part: list[list[Any]] = []
        if not d or (d["minutes"] <= 0 and not items):
            out.append(base + [0, "—", "—", "—", 0, "", "", ""])
            continue
        for it in items:
            lc = it.get("last_comment")
            part.append(base + [d["minutes"], areas.get(it["area_id"], "—"), stations.get(it["station_id"], "—"),
                               reasons.get(it["reason_id"], "—"), it["minutes"], it.get("note") or "", it.get("created_by_name") or "",
                               f"{lc['text']} · {lc.get('author') or lc['user_name']}" if lc else ""])
        rest = round(d["minutes"] - d["described"], 1)
        if rest > 0.5:
            part.append(base + [d["minutes"], "—", "—", "не описан", rest, "", "", ""])
        # значения интервала (минуты, план, факт, ±, такт-тайм, простой) пишутся один раз, на первой строке: так столбец суммируется верно
        for i, row in enumerate(part):
            if i:
                row[5:11] = [""] * 6
            out.append(row)
    intervals = [(f"{_hhmm(h['start_min'])}-{_hhmm(h['end_min'] if h['end_min'] is not None else h['start_min'])}", h["plan"], h["fact"])
                 for h in hours]
    folder = safe_name(group if label in ("", "Main Line") else f"{group}_{label}")
    return {"line": line, "folder": folder, "fname": f"{folder}_{day.isoformat()}.xlsx", "rows": out, "intervals": intervals, "n": len(hours)}


async def build_day_report(pool: asyncpg.Pool, screen_id: int, day: date) -> tuple[bytes, str, str, int] | None:
    """Отчёт дня по экрану: (книга, имя папки линии, имя файла, число интервалов с планом или фактом); None, если экрана нет."""
    d = await build_day_data(pool, screen_id, day)
    if d is None:
        return None
    return build_day_workbook(d["line"], day, d["rows"], d["intervals"]), d["folder"], d["fname"], d["n"]


@router.get("/screens/{screen_id}/day.xlsx")
async def day_xlsx(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date"), src: str = Query(default="reports")) -> Response:
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
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name(src, fname))}"},
    )


@router.get("/screens/{screen_id}/day-preview")
async def day_preview(screen_id: int, request: Request, day: date = Query(alias="date")) -> dict[str, Any]:
    """Что будет в отчёте дня: заголовки и строки таблицы листа «День» и интервалы для графика план/факт."""
    d = await build_day_data(request.app.state.pool, screen_id, day)
    if d is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    return {
        "line": d["line"], "date": day.isoformat(), "file": download_name("reports", d["fname"]), "headers": DAY_HEADERS, "rows": d["rows"],
        "intervals": [{"label": lbl, "plan": p, "fact": f} for lbl, p, f in d["intervals"]],
    }
