"""Отчёты по линии за период (неделя, месяц, любые даты): Excel с графиками, данные для вкладки «Графики» и выгрузка отдельного графика."""
from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote

import asyncpg
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from downtime import _screen_path
from history import download_name, safe_name

router = APIRouter(prefix="/api/kpi")

MAX_DAYS = 366
UNDESCRIBED = "не описан"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GRID = {"visible": True, "line": {"color": "#D9D9D9"}}
KIND_FILE = {"plan_fact": "план-факт", "downtime": "простои", "reasons": "причины", "stations": "станции"}
KINDS = {"plan_fact": "план-факт по дням", "downtime": "простои по дням", "reasons": "парето причин", "stations": "станции"}


async def _load(pool: asyncpg.Pool, screen_id: int, d1: date, d2: date) -> dict[str, Any]:
    days = await pool.fetch(
        "SELECT prod_date, sum(plan)::int AS plan, sum(fact)::int AS fact FROM kpi_hourly "
        "WHERE screen_id = $1 AND prod_date BETWEEN $2 AND $3 AND (plan > 0 OR fact > 0) GROUP BY prod_date ORDER BY prod_date",
        screen_id, d1, d2,
    )
    dt_minutes = {r["prod_date"]: float(r["m"]) for r in await pool.fetch(
        "SELECT prod_date, sum(minutes) AS m FROM kpi_downtimes WHERE screen_id = $1 AND prod_date BETWEEN $2 AND $3 AND source = 'plan' "
        "GROUP BY prod_date", screen_id, d1, d2)}
    items = await pool.fetch(
        "SELECT d.prod_date, d.idx, i.area_id, i.station_id, i.reason_id, i.minutes, i.note, i.created_by_name "
        "FROM kpi_downtime_items i JOIN kpi_downtimes d ON d.id = i.downtime_id "
        "WHERE d.screen_id = $1 AND d.prod_date BETWEEN $2 AND $3 ORDER BY d.prod_date, d.idx, i.id",
        screen_id, d1, d2,
    )
    starts = {(r["prod_date"], r["idx"]): (r["start_min"], r["end_min"]) for r in await pool.fetch(
        "SELECT prod_date, idx, start_min, end_min FROM kpi_hourly WHERE screen_id = $1 AND prod_date BETWEEN $2 AND $3",
        screen_id, d1, d2)}
    return {
        "days": days, "dt_minutes": dt_minutes, "items": items, "starts": starts,
        "areas": {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_areas")},
        "stations": {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_stations")},
        "reasons": {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_reasons")},
    }


def _hhmm(m: int | None) -> str:
    return "" if m is None else f"{(m // 60) % 24:02d}:{m % 60:02d}"


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    """Свод для графиков и отчётов: строки по дням, причины и станции по убыванию минут (с остатком «не описан»)."""
    days, dt_minutes, items = data["days"], data["dt_minutes"], data["items"]
    areas, stations, reasons = data["areas"], data["stations"], data["reasons"]
    described: dict[date, float] = {}
    by_reason: dict[str, float] = {}
    by_station: dict[tuple[str, str], float] = {}
    for it in items:
        m = float(it["minutes"])
        described[it["prod_date"]] = described.get(it["prod_date"], 0.0) + m
        rn = reasons.get(it["reason_id"], "—")
        by_reason[rn] = by_reason.get(rn, 0.0) + m
        key = (areas.get(it["area_id"], "—"), stations.get(it["station_id"], "—"))
        by_station[key] = by_station.get(key, 0.0) + m
    rest_total = sum(r for d, m in dt_minutes.items() if (r := round(m - described.get(d, 0.0), 1)) > 0.5)
    if rest_total > 0:
        by_reason[UNDESCRIBED] = round(rest_total, 1)
        by_station[("—", UNDESCRIBED)] = round(rest_total, 1)
    rows = []
    for d in days:
        day, plan, fact = d["prod_date"], d["plan"], d["fact"]
        dtm = round(dt_minutes.get(day, 0.0), 1)
        desc = round(min(described.get(day, 0.0), dtm), 1) if dtm else 0.0
        rows.append({"date": day, "plan": plan, "fact": fact, "pct": (fact / plan) if plan else None, "downtime": dtm, "described": desc})
    return {
        "days": rows,
        "reasons": sorted(((n, round(m, 1)) for n, m in by_reason.items()), key=lambda x: -x[1]),
        "stations": sorted(((a, s, round(m, 1)) for (a, s), m in by_station.items()), key=lambda x: -x[2]),
    }


# ---------- диаграммы Excel (общие для отчёта за период и выгрузки отдельного графика) ----------

def _chart_plan_fact(wb, sheet: str, n: int, c_plan=1, c_fact=2, c_pct=4, r0=0):
    cats = [sheet, r0 + 1, 0, r0 + n, 0]
    col = wb.add_chart({"type": "column"})
    col.add_series({"name": "План", "categories": cats, "values": [sheet, r0 + 1, c_plan, r0 + n, c_plan], "fill": {"color": "#8FA3BF"}, "gap": 60})
    col.add_series({"name": "Факт", "categories": cats, "values": [sheet, r0 + 1, c_fact, r0 + n, c_fact], "fill": {"color": "#2F80ED"}})
    ln = wb.add_chart({"type": "line"})
    ln.add_series({"name": "% выполнения", "categories": cats, "values": [sheet, r0 + 1, c_pct, r0 + n, c_pct], "y2_axis": True,
                   "line": {"color": "#27AE60", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
    col.combine(ln)
    col.set_title({"name": "План, факт и % выполнения по дням"})
    col.set_x_axis({"num_format": "dd.mm", "num_font": {"rotation": -45}})
    col.set_y_axis({"name": "Кузовов", "major_gridlines": GRID})
    ln.set_y2_axis({"name": "% выполнения", "num_format": "0%"})
    col.set_legend({"position": "bottom"})
    col.set_size({"width": 860, "height": 340})
    return col


def _chart_downtime(wb, sheet: str, n: int, c_min=5, r0=0):
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": "Простой, мин", "categories": [sheet, r0 + 1, 0, r0 + n, 0], "values": [sheet, r0 + 1, c_min, r0 + n, c_min],
                   "fill": {"color": "#E88B8B"}, "gap": 60})
    ch.set_title({"name": "Простой по дням, мин"})
    ch.set_x_axis({"num_format": "dd.mm", "num_font": {"rotation": -45}})
    ch.set_y_axis({"name": "Минут", "major_gridlines": GRID})
    ch.set_legend({"none": True})
    ch.set_size({"width": 640, "height": 300})
    return ch


def _chart_pareto(wb, sheet: str, n: int, c_min=1, c_cum=3, r0=0):
    cats = [sheet, r0 + 1, 0, r0 + n, 0]
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": "Минут", "categories": cats, "values": [sheet, r0 + 1, c_min, r0 + n, c_min], "fill": {"color": "#E88B8B"}, "gap": 50})
    ln = wb.add_chart({"type": "line"})
    ln.add_series({"name": "Накопленная доля", "categories": cats, "values": [sheet, r0 + 1, c_cum, r0 + n, c_cum], "y2_axis": True,
                   "line": {"color": "#4F5B6B", "width": 2}, "marker": {"type": "circle", "size": 5}})
    ch.combine(ln)
    ch.set_title({"name": "Парето: простои по причинам (топ-10)"})
    ch.set_y_axis({"name": "Минут", "major_gridlines": GRID})
    ln.set_y2_axis({"num_format": "0%", "max": 1, "min": 0})
    ch.set_x_axis({"num_font": {"rotation": -30}})
    ch.set_legend({"position": "bottom"})
    ch.set_size({"width": 640, "height": 340})
    return ch


def _chart_stations(wb, sheet: str, n: int, c_name=1, c_min=2, r0=0):
    ch = wb.add_chart({"type": "bar"})
    ch.add_series({"name": "Минут", "categories": [sheet, r0 + 1, c_name, r0 + n, c_name], "values": [sheet, r0 + 1, c_min, r0 + n, c_min],
                   "fill": {"color": "#2F80ED"}, "data_labels": {"value": True}, "gap": 40})
    ch.set_title({"name": "Простои по станциям (топ-15), мин"})
    ch.set_y_axis({"reverse": True})
    ch.set_x_axis({"major_gridlines": GRID})
    ch.set_legend({"none": True})
    ch.set_size({"width": 640, "height": 420})
    return ch


def _fmts(wb) -> dict[str, Any]:
    tile = {"font_size": 18, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}
    return {
        "head": wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"}),
        "date": wb.add_format({"num_format": "dd.mm.yyyy", "align": "left"}),
        "pct": wb.add_format({"num_format": "0.0%"}),
        "n1": wb.add_format({"num_format": "0.0"}),
        "bold": wb.add_format({"bold": True}),
        "bpct": wb.add_format({"bold": True, "num_format": "0.0%"}),
        "bn1": wb.add_format({"bold": True, "num_format": "0.0"}),
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "section": wb.add_format({"bold": True, "font_size": 12}),
        "tile_l": wb.add_format({"font_size": 9, "bold": True, "font_color": "#6B7785", "bg_color": "#F2F4F7", "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}),
        "tile_v": wb.add_format(tile),
        "tile_p": wb.add_format({**tile, "num_format": "0.0%"}),
        "tile_1": wb.add_format({**tile, "num_format": "0.0"}),
        "tile_ok": wb.add_format({**tile, "font_color": "#1E8E5A"}),
        "tile_bad": wb.add_format({**tile, "num_format": "0.0", "font_color": "#C0392B"}),
    }


def _write_days(ws, f, sm_days: list[dict[str, Any]], full: bool, r0: int = 0) -> None:
    heads = ["Дата", "План", "Факт", "±", "% выполнения"] + (["Простой, мин", "Описано, мин", "Не описано, мин"] if full else [])
    ws.write_row(r0, 0, heads, f["head"])
    for r, d in enumerate(sm_days, start=r0 + 1):
        ws.write_datetime(r, 0, datetime.combine(d["date"], time()), f["date"])
        ws.write(r, 1, d["plan"])
        ws.write(r, 2, d["fact"])
        ws.write_formula(r, 3, f"=C{r + 1}-B{r + 1}", None, d["fact"] - d["plan"])
        ws.write_formula(r, 4, f'=IF(B{r + 1}=0,"",C{r + 1}/B{r + 1})', f["pct"], d["pct"] if d["pct"] is not None else "")
        if full:
            ws.write(r, 5, d["downtime"], f["n1"])
            ws.write(r, 6, d["described"], f["n1"])
            ws.write_formula(r, 7, f"=MAX(0,F{r + 1}-G{r + 1})", f["n1"], max(0.0, round(d["downtime"] - d["described"], 1)))
    if r0 == 0:
        ws.set_column(0, 0, 12)
        ws.set_column(1, 7, 14)
        ws.freeze_panes(1, 0)


def _write_reasons(rs, f, rows: list[tuple[str, float]], r0: int = 0) -> None:
    rs.write_row(r0, 0, ["Причина", "Минут", "Доля", "Накопленная доля"], f["head"])
    total, cum = sum(m for _, m in rows), 0.0
    for r, (name, m) in enumerate(rows, start=r0 + 1):
        cum += m
        rs.write(r, 0, name)
        rs.write(r, 1, m, f["n1"])
        rs.write(r, 2, m / total if total else 0, f["pct"])
        rs.write(r, 3, cum / total if total else 0, f["pct"])
    if r0 == 0:
        rs.set_column(0, 0, 32)
        rs.set_column(1, 3, 16)
        rs.freeze_panes(1, 0)


def _write_stations(ss, f, rows: list[tuple[str, str, float]], r0: int = 0) -> None:
    ss.write_row(r0, 0, ["Участок", "Станция", "Минут", "Доля"], f["head"])
    total = sum(m for _, _, m in rows)
    for r, (area, st, m) in enumerate(rows, start=r0 + 1):
        ss.write(r, 0, area)
        ss.write(r, 1, st)
        ss.write(r, 2, m, f["n1"])
        ss.write(r, 3, m / total if total else 0, f["pct"])
    if r0 == 0:
        ss.set_column(0, 1, 24)
        ss.set_column(2, 3, 12)
        ss.freeze_panes(1, 0)


def _rows_for(px: int) -> int:
    """Сколько строк листа занимает диаграмма высотой px пикселей (строка 20 px) с отступом."""
    return -(-px // 20) + 2


def build_period_workbook(line: str, d1: date, d2: date, data: dict[str, Any]) -> bytes:
    """Книга по виду как вкладка «Отчёты»: плитки итогов, график, затем таблицы по дням, причинам и станциям на одном листе «Отчёт»;
    полный список простоев на листе «Простои»."""
    sm = summarize(data)
    items, areas, stations, reasons, starts = data["items"], data["areas"], data["stations"], data["reasons"], data["starts"]
    days, reason_rows, station_rows = sm["days"], sm["reasons"], sm["stations"]
    n = len(days)

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    ws = wb.add_worksheet("Отчёт")
    ls = wb.add_worksheet("Простои")
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 16)
    ws.set_column(2, 7, 15)
    tp, tf = sum(d["plan"] for d in days), sum(d["fact"] for d in days)
    tdt, tdesc = round(sum(d["downtime"] for d in days), 1), round(sum(d["described"] for d in days), 1)

    # 1. плитки итогов (как на экране)
    ws.write(0, 0, f"{line}: отчёт за период {d1.strftime('%d.%m.%Y')} — {d2.strftime('%d.%m.%Y')}", f["title"])
    tiles = [("Дней с данными", n, "tile_v"), ("План", tp, "tile_v"), ("Факт", tf, "tile_ok"),
             ("Выполнение", (tf / tp) if tp else "", "tile_p"), ("Простой, мин", tdt, "tile_bad"), ("Не описано, мин", round(tdt - tdesc, 1), "tile_1")]
    for c, (label, val, fmt) in enumerate(tiles):
        ws.write(2, c, label, f["tile_l"])
        ws.write(3, c, val, f[fmt])
    ws.set_row(2, 18)
    ws.set_row(3, 32)

    # 2. график план/факт/% по дням; под ним таблицы
    chart_row = 5
    row = chart_row + (_rows_for(340) if n else 0)

    ws.write(row, 0, "По дням", f["section"])
    t_days = row + 1
    _write_days(ws, f, days, True, r0=t_days)
    if n:
        ws.insert_chart(chart_row, 0, _chart_plan_fact(wb, "Отчёт", n, r0=t_days))
        t = t_days + n + 1
        first, last = t_days + 2, t
        ws.write(t, 0, "Итого", f["bold"])
        ws.write_formula(t, 1, f"=SUM(B{first}:B{last})", f["bold"], tp)
        ws.write_formula(t, 2, f"=SUM(C{first}:C{last})", f["bold"], tf)
        ws.write_formula(t, 3, f"=C{t + 1}-B{t + 1}", f["bold"], tf - tp)
        ws.write_formula(t, 4, f'=IF(B{t + 1}=0,"",C{t + 1}/B{t + 1})', f["bpct"], (tf / tp) if tp else "")
        ws.write_formula(t, 5, f"=SUM(F{first}:F{last})", f["bn1"], tdt)
        ws.write_formula(t, 6, f"=SUM(G{first}:G{last})", f["bn1"], tdesc)
        ws.write_formula(t, 7, f"=SUM(H{first}:H{last})", f["bn1"], round(tdt - tdesc, 1))
        ws.insert_chart(t_days, 9, _chart_downtime(wb, "Отчёт", n, r0=t_days))
    row = t_days + max(n + 3, _rows_for(300)) + 1

    ws.write(row, 0, "Причины простоя", f["section"])
    t_reasons = row + 1
    _write_reasons(ws, f, reason_rows, r0=t_reasons)
    if reason_rows:
        ws.insert_chart(t_reasons, 9, _chart_pareto(wb, "Отчёт", min(len(reason_rows), 10), r0=t_reasons))
    row = t_reasons + max(len(reason_rows) + 3, _rows_for(340)) + 1

    ws.write(row, 0, "Простой по станциям", f["section"])
    t_st = row + 1
    _write_stations(ws, f, station_rows, r0=t_st)
    if station_rows:
        ws.insert_chart(t_st, 9, _chart_stations(wb, "Отчёт", min(len(station_rows), 15), r0=t_st))

    # 3. полный список простоев (на экране его нет)
    heads = ["Дата", "№", "Начало", "Окончание", "Участок", "Станция", "Причина", "Минут", "Описание", "Внёс"]
    ls.write_row(0, 0, heads, f["head"])
    for r, it in enumerate(items, start=1):
        st = starts.get((it["prod_date"], it["idx"]), (None, None))
        ls.write_datetime(r, 0, datetime.combine(it["prod_date"], time()), f["date"])
        ls.write(r, 1, it["idx"])
        ls.write(r, 2, _hhmm(st[0]))
        ls.write(r, 3, _hhmm(st[1]))
        ls.write(r, 4, areas.get(it["area_id"], "—"))
        ls.write(r, 5, stations.get(it["station_id"], "—"))
        ls.write(r, 6, reasons.get(it["reason_id"], "—"))
        ls.write(r, 7, float(it["minutes"]), f["n1"])
        ls.write(r, 8, it["note"] or "")
        ls.write(r, 9, it["created_by_name"] or "")
    for c, w in enumerate([12, 5, 9, 11, 20, 20, 26, 8, 40, 24]):
        ls.set_column(c, c, w)
    ls.freeze_panes(1, 0)
    ls.autofilter(0, 0, max(len(items), 1), len(heads) - 1)
    wb.close()
    return buf.getvalue()


def build_chart_workbook(kind: str, line: str, d1: date, d2: date, data: dict[str, Any]) -> bytes:
    """Книга с одним графиком: лист «Данные» (таблица, из которой построен график) и сама диаграмма Excel."""
    sm = summarize(data)
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    ws = wb.add_worksheet("Данные")
    note = f"{line}, период {d1.strftime('%d.%m.%Y')} — {d2.strftime('%d.%m.%Y')}"
    n = 0
    if kind in ("plan_fact", "downtime"):
        days = sm["days"]
        n = len(days)
        if kind == "plan_fact":
            _write_days(ws, f, days, False)
            chart = _chart_plan_fact(wb, "Данные", n) if n else None
        else:
            ws.write_row(0, 0, ["Дата", "Простой, мин", "Описано, мин", "Не описано, мин"], f["head"])
            for r, d in enumerate(days, start=1):
                ws.write_datetime(r, 0, datetime.combine(d["date"], time()), f["date"])
                ws.write(r, 1, d["downtime"], f["n1"])
                ws.write(r, 2, d["described"], f["n1"])
                ws.write_formula(r, 3, f"=MAX(0,B{r + 1}-C{r + 1})", f["n1"], max(0.0, round(d["downtime"] - d["described"], 1)))
            ws.set_column(0, 3, 16)
            ws.freeze_panes(1, 0)
            chart = _chart_downtime(wb, "Данные", n, c_min=1) if n else None
    elif kind == "reasons":
        _write_reasons(ws, f, sm["reasons"])
        n = len(sm["reasons"])
        chart = _chart_pareto(wb, "Данные", min(n, 10)) if n else None
    else:
        _write_stations(ws, f, sm["stations"])
        n = len(sm["stations"])
        chart = _chart_stations(wb, "Данные", min(n, 15)) if n else None
    ws.write(n + 3, 0, note, f["bold"])
    if chart:
        ws.insert_chart("G2", chart)
    wb.close()
    return buf.getvalue()


def _check_period(d1: date, d2: date) -> None:
    if d2 < d1:
        raise HTTPException(status_code=400, detail="Дата «по» раньше даты «с»")
    if (d2 - d1).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Период не больше {MAX_DAYS} дней")


async def _screen_line(pool: asyncpg.Pool, screen_id: int) -> tuple[str, str]:
    """(подпись линии «GWM › Main Line», имя папки/файла)."""
    scr = await pool.fetchrow("SELECT name, template, bindings FROM kpi_screens WHERE id = $1", screen_id)
    if scr is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    group, label = _screen_path(scr["template"], scr["name"], scr["bindings"])
    return f"{group} › {label}", safe_name(group if label in ("", "Main Line") else f"{group}_{label}")


async def build_period_report(pool: asyncpg.Pool, screen_id: int, d1: date, d2: date) -> tuple[bytes, str, str, int] | None:
    """(книга, имя папки линии, имя файла, число дней с данными); None, если экрана нет."""
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        return None
    line, folder = await _screen_line(pool, screen_id)
    data = await _load(pool, screen_id, d1, d2)
    return build_period_workbook(line, d1, d2, data), folder, f"{folder}_{d1.isoformat()}_{d2.isoformat()}.xlsx", len(data["days"])


def _xlsx_response(data: bytes, fname: str, src: str = "reports") -> Response:
    return Response(content=data, media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name(src, fname))}"})


@router.get("/screens/{screen_id}/period.xlsx")
async def period_xlsx(
    screen_id: int, request: Request,
    date_from: date = Query(alias="from"), date_to: date = Query(alias="to"), src: str = Query(default="reports"),
) -> Response:
    """Отчёт линии за период в Excel с графиками; доступен всем без входа."""
    _check_period(date_from, date_to)
    rep = await build_period_report(request.app.state.pool, screen_id, date_from, date_to)
    if rep is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    data, _, fname, days = rep
    if days == 0:
        raise HTTPException(status_code=404, detail="За выбранный период нет данных по этой линии")
    return _xlsx_response(data, fname, src)


@router.get("/charts")
async def charts_data(
    request: Request, screen_id: int = Query(),
    date_from: date = Query(alias="from"), date_to: date = Query(alias="to"),
) -> dict[str, Any]:
    """Данные для графиков вкладки «Графики»: по дням, причины и станции за период; доступны всем."""
    _check_period(date_from, date_to)
    pool: asyncpg.Pool = request.app.state.pool
    line, _ = await _screen_line(pool, screen_id)
    sm = summarize(await _load(pool, screen_id, date_from, date_to))
    return {
        "line": line, "from": date_from.isoformat(), "to": date_to.isoformat(),
        "days": [{**d, "date": d["date"].isoformat()} for d in sm["days"]],
        "reasons": [{"name": n, "min": m} for n, m in sm["reasons"]],
        "stations": [{"area": a, "station": s, "min": m} for a, s, m in sm["stations"]],
    }


@router.get("/screens/{screen_id}/chart.xlsx")
async def chart_xlsx(
    screen_id: int, request: Request, kind: str = Query(),
    date_from: date = Query(alias="from"), date_to: date = Query(alias="to"), src: str = Query(default="charts"),
) -> Response:
    """Excel с данными и диаграммой одного графика (plan_fact, downtime, reasons, stations)."""
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Неизвестный график")
    _check_period(date_from, date_to)
    pool: asyncpg.Pool = request.app.state.pool
    line, folder = await _screen_line(pool, screen_id)
    data = await _load(pool, screen_id, date_from, date_to)
    if not data["days"] and not data["items"]:
        raise HTTPException(status_code=404, detail="За выбранный период нет данных по этой линии")
    book = build_chart_workbook(kind, line, date_from, date_to, data)
    return _xlsx_response(book, f"{folder}_{KIND_FILE[kind]}_{date_from.isoformat()}_{date_to.isoformat()}.xlsx", src)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)
