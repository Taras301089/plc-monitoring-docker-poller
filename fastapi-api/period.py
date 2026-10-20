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

import dayflags
from labels import label as ui_label
from downtime import _screen_path, owner_label, owner_status
from history import cumulative_day, download_name, safe_name
from responsibility import owner_split, write_responsibility

router = APIRouter(prefix="/api/kpi")

MAX_DAYS = 366
UNDESCRIBED = "не описан"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GRID = {"visible": True, "line": {"color": "#D9D9D9"}}
GRAY_BG = "#ECEEF2"
KIND_FILE = {"plan_fact": "план-факт", "downtime": "простои", "reasons": "причины", "stations": "станции", "cumulative": "накопленный"}
KINDS = {"plan_fact": "план-факт по дням", "downtime": "простои по дням", "reasons": "парето причин", "stations": "станции", "cumulative": "накопленное производство за день"}


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
        # ответ отдела (только того, кто сейчас ответственный): по нему статус и деление минут «Частично» (responsibility.owner_split)
        "SELECT d.prod_date, d.idx, i.area_id, i.station_id, i.reason_id, i.minutes, i.note, i.owner, i.created_by_name, "
        "rp.answer, rp.minutes AS resp_minutes "
        "FROM kpi_downtime_items i JOIN kpi_downtimes d ON d.id = i.downtime_id "
        "LEFT JOIN kpi_downtime_responses rp ON rp.item_id = i.id AND rp.department = i.owner "
        "WHERE d.screen_id = $1 AND d.prod_date BETWEEN $2 AND $3 ORDER BY d.prod_date, d.idx, i.id",
        screen_id, d1, d2,
    )
    starts = {(r["prod_date"], r["idx"]): (r["start_min"], r["end_min"]) for r in await pool.fetch(
        "SELECT prod_date, idx, start_min, end_min FROM kpi_hourly WHERE screen_id = $1 AND prod_date BETWEEN $2 AND $3",
        screen_id, d1, d2)}
    # дни без выпуска (единое правило dayflags): автоматический простой и «не описано» не считаются, остаются только описанные людьми минуты
    no_output = {d for (_, d), v in (await dayflags.day_states(pool, d1, d2, screen_id)).items() if v["state"] == dayflags.NO_OUTPUT}
    for d in no_output:
        kept = round(sum(float(i["minutes"]) for i in items if i["prod_date"] == d), 1)
        if kept:
            dt_minutes[d] = kept
        else:
            dt_minutes.pop(d, None)
    return {
        "days": days, "dt_minutes": dt_minutes, "items": items, "starts": starts, "no_output": no_output,
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
        rows.append({"date": day, "plan": plan, "fact": fact, "pct": (fact / plan) if plan else None, "downtime": dtm, "described": desc,
                     "no_output": day in data.get("no_output", ())})
    # Итоги периода: дни без выпуска (закрытый день с фактом 0, без отметки «простой весь день») в план, факт, % и число дней не входят.
    # Простой и «описано» считаются по всем дням: описанные людьми минуты дня без выпуска остаются в статистике и в Парето причин
    counted = [r for r in rows if not r["no_output"]]
    dt_all = round(sum(r["downtime"] for r in rows), 1)
    totals = {"days": len(counted), "plan": sum(r["plan"] for r in counted), "fact": sum(r["fact"] for r in counted),
              "downtime": dt_all, "described": round(sum(r["described"] for r in rows), 1)}
    return {
        "days": rows,
        "totals": totals,
        "reasons": sorted(((n, round(m, 1)) for n, m in by_reason.items()), key=lambda x: -x[1]),
        "stations": sorted(((a, s, round(m, 1)) for (a, s), m in by_station.items()), key=lambda x: -x[2]),
    }


# ---------- диаграммы Excel (общие для отчёта за период и выгрузки отдельного графика) ----------

def _chart_plan_fact(wb, sheet: str, n: int, c_plan=1, c_fact=2, c_pct=4, r0=0):
    cats = [sheet, r0 + 1, 0, r0 + n, 0]
    col = wb.add_chart({"type": "column"})
    col.add_series({"name": "План", "categories": cats, "values": [sheet, r0 + 1, c_plan, r0 + n, c_plan], "fill": {"color": "#8FA3BF"}, "gap": 60,
                    "data_labels": {"value": True, "font": {"color": "#6B7785"}}})
    col.add_series({"name": "Факт", "categories": cats, "values": [sheet, r0 + 1, c_fact, r0 + n, c_fact], "fill": {"color": "#2F80ED"},
                    "data_labels": {"value": True}})
    ln = wb.add_chart({"type": "line"})
    ln.add_series({"name": "% выполнения", "categories": cats, "values": [sheet, r0 + 1, c_pct, r0 + n, c_pct], "y2_axis": True,
                   "line": {"color": "#27AE60", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
    col.combine(ln)
    col.set_title({"name": ui_label("chart_plan_fact")})
    col.set_x_axis({"num_format": "dd.mm", "num_font": {"rotation": -45}})
    col.set_y_axis({"name": "Кузовов", "major_gridlines": GRID})
    ln.set_y2_axis({"name": "% выполнения", "num_format": "0%"})
    col.set_legend({"position": "bottom"})
    col.set_size({"width": 860, "height": 340})
    return col


def _chart_downtime(wb, sheet: str, n: int, c_min=5, r0=0):
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": ui_label("down_min"), "categories": [sheet, r0 + 1, 0, r0 + n, 0], "values": [sheet, r0 + 1, c_min, r0 + n, c_min],
                   "fill": {"color": "#E88B8B"}, "gap": 60})
    ch.set_title({"name": ui_label("chart_down_days")})
    ch.set_x_axis({"num_format": "dd.mm", "num_font": {"rotation": -45}})
    ch.set_y_axis({"name": "Минут", "major_gridlines": GRID})
    ch.set_legend({"none": True})
    ch.set_size({"width": 640, "height": 300})
    return ch


def _chart_pareto(wb, sheet: str, n: int, c_min=1, c_cum=3, r0=0):
    cats = [sheet, r0 + 1, 0, r0 + n, 0]
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": ui_label("down_min"), "categories": cats, "values": [sheet, r0 + 1, c_min, r0 + n, c_min], "fill": {"color": "#E88B8B"}, "gap": 50})
    ln = wb.add_chart({"type": "line"})
    ln.add_series({"name": "Накопленный %", "categories": cats, "values": [sheet, r0 + 1, c_cum, r0 + n, c_cum], "y2_axis": True,
                   "line": {"color": "#4F5B6B", "width": 2}, "marker": {"type": "circle", "size": 5}})
    ch.combine(ln)
    ch.set_title({"name": ui_label("chart_pareto")})
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
    ch.set_title({"name": ui_label("chart_down_stations")})
    ch.set_y_axis({"reverse": True})
    ch.set_x_axis({"major_gridlines": GRID})
    ch.set_legend({"none": True})
    ch.set_size({"width": 640, "height": 420})
    return ch


def _chart_cumulative(wb, sheet: str, n: int, r0=0):
    """Как на экране: накопленные план и факт линиями, разница столбцами на той же шкале
    (отставание от плана красным, опережение зелёным)."""
    cats = [sheet, r0 + 1, 0, r0 + n, 0]
    col = wb.add_chart({"type": "column", "subtype": "stacked"})
    col.add_series({"name": "Отставание от плана", "categories": cats, "values": [sheet, r0 + 1, 7, r0 + n, 7], "fill": {"color": "#E88B8B"}, "gap": 80})
    col.add_series({"name": "Опережение плана", "categories": cats, "values": [sheet, r0 + 1, 6, r0 + n, 6], "fill": {"color": "#27AE60"}})
    ln = wb.add_chart({"type": "line"})
    ln.add_series({"name": "Накопленный план", "categories": cats, "values": [sheet, r0 + 1, 3, r0 + n, 3],
                   "line": {"color": "#8FA3BF", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
    ln.add_series({"name": "Накопленный факт", "categories": cats, "values": [sheet, r0 + 1, 4, r0 + n, 4],
                   "line": {"color": "#2F80ED", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
    col.combine(ln)
    col.set_title({"name": ui_label("chart_cumulative")})
    col.set_x_axis({"name": "Интервал", "label_position": "low"})
    col.set_y_axis({"name": "Кузовов", "major_gridlines": GRID})
    col.set_legend({"position": "bottom"})
    col.set_size({"width": 860, "height": 340})
    return col


def _write_cumulative(ws, f, rows: list[dict[str, Any]]) -> None:
    """Таблица данных графика «накопленное производство за день» (A:H); столбцы G и H делят разницу на опережение и отставание."""
    ws.write_row(0, 0, ["Интервал", "План", "Факт", "Накопленный план", "Накопленный факт", "Разница (факт − план)",
                        "Опережение плана", "Отставание от плана"], f["head"])
    for r, d in enumerate(rows, start=1):
        ws.write(r, 0, d["label"])
        ws.write(r, 1, d["plan"])
        ws.write(r, 2, d["fact"])
        ws.write_formula(r, 3, f"=SUM(B$2:B{r + 1})", None, d["cum_plan"])
        ws.write_formula(r, 4, f"=SUM(C$2:C{r + 1})", None, d["cum_fact"])
        ws.write_formula(r, 5, f"=E{r + 1}-D{r + 1}", None, d["diff"])
        ws.write_formula(r, 6, f"=MAX(0,F{r + 1})", None, max(0, d["diff"]))
        ws.write_formula(r, 7, f"=MIN(0,F{r + 1})", None, min(0, d["diff"]))
    ws.set_column(0, 0, 14)
    ws.set_column(1, 7, 18)
    ws.freeze_panes(1, 0)


def build_cumulative_workbook(line: str, day: date, rows: list[dict[str, Any]]) -> bytes:
    """Книга одного графика «накопленное производство за день»: таблица данных и диаграмма как на экране."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    ws = wb.add_worksheet("Данные")
    _write_cumulative(ws, f, rows)
    n = len(rows)
    ws.write(n + 3, 0, f"{line}, {day.strftime('%d.%m.%Y')}", f["bold"])
    if n:
        ws.insert_chart("J2", _chart_cumulative(wb, "Данные", n))
    wb.close()
    return buf.getvalue()


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
        "dim": wb.add_format({"font_color": "#6B7785", "align": "right"}),
        # строки дней без выпуска: серая заливка, в итоги не входят (как на экране)
        "g_date": wb.add_format({"num_format": "dd.mm.yyyy", "align": "left", "bg_color": GRAY_BG, "font_color": "#6B7785"}),
        "g_n": wb.add_format({"bg_color": GRAY_BG, "font_color": "#6B7785"}),
        "g_pct": wb.add_format({"num_format": "0.0%", "bg_color": GRAY_BG, "font_color": "#6B7785"}),
        "g_dim": wb.add_format({"font_color": "#6B7785", "align": "right", "bg_color": GRAY_BG}),
    }


def _write_days(ws, f, sm_days: list[dict[str, Any]], full: bool, r0: int = 0) -> None:
    heads = ["Дата", "План", "Факт", "±", "% выполнения"] + ([ui_label("down_min"), "Описано, мин", ui_label("tile_undescribed")] if full else [])
    ws.write_row(r0, 0, heads, f["head"])
    for r, d in enumerate(sm_days, start=r0 + 1):
        out = bool(d.get("no_output"))   # день без выпуска: серая строка, в итоги не входит
        ws.write_datetime(r, 0, datetime.combine(d["date"], time()), f["g_date" if out else "date"])
        ws.write(r, 1, d["plan"], f["g_n"] if out else None)
        ws.write(r, 2, d["fact"], f["g_n"] if out else None)
        if out:   # «±» не показывается
            ws.write_blank(r, 3, None, f["g_n"])
        else:
            ws.write_formula(r, 3, f"=C{r + 1}-B{r + 1}", None, d["fact"] - d["plan"])
        ws.write_formula(r, 4, f'=IF(B{r + 1}=0,"",C{r + 1}/B{r + 1})', f["g_pct" if out else "pct"], d["pct"] if d["pct"] is not None else "")
        if full and out and not d["downtime"]:
            # простой дня без выпуска не считается; текст в SUM не входит
            ws.write(r, 5, dayflags.NO_OUTPUT_LABEL, f["g_dim"])
            ws.write(r, 6, "—", f["g_dim"])
            ws.write(r, 7, "—", f["g_dim"])
        elif full:
            ws.write(r, 5, d["downtime"], f["n1"])
            ws.write(r, 6, d["described"], f["n1"])
            ws.write_formula(r, 7, f"=MAX(0,F{r + 1}-G{r + 1})", f["n1"], max(0.0, round(d["downtime"] - d["described"], 1)))
    if r0 == 0:
        ws.set_column(0, 0, 12)
        ws.set_column(1, 7, 14)
        ws.freeze_panes(1, 0)


def _write_reasons(rs, f, rows: list[tuple[str, float]], r0: int = 0) -> None:
    rs.write_row(r0, 0, ["Причина", "Минут", "Доля", "Накопленный %"], f["head"])
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


def write_period_section(wb, ws, f: dict[str, Any], title: str, data: dict[str, Any], cum: list[dict[str, Any]] | None = None,
                         r0: int = 0, cum_sheet: str = "График", days_sheet: str = "График дней") -> int:
    """Раздел отчёта за период на листе ws начиная со строки r0: заголовок, плитки итогов, график, таблицы по дням, причинам и станциям
    (и график накопленного производства, если передан cum). Возвращает первую свободную строку под разделом."""
    sm = summarize(data)
    days, reason_rows, station_rows = sm["days"], sm["reasons"], sm["stations"]
    n = len(days)
    counted = [d for d in days if not d.get("no_output")]   # дни без выпуска остаются в таблице, но не в итогах и не на графике
    sheet = ws.get_name()
    tp, tf = sm["totals"]["plan"], sm["totals"]["fact"]
    tdt, tdesc = sm["totals"]["downtime"], sm["totals"]["described"]

    # 1. плитки итогов (как на экране)
    ws.write(r0, 0, title, f["title"])
    tiles = [(ui_label("tile_days"), sm["totals"]["days"], "tile_v"), (ui_label("tile_plan"), tp, "tile_v"), (ui_label("tile_fact"), tf, "tile_ok"),
             (ui_label("tile_exec"), (tf / tp) if tp else "", "tile_p"), (ui_label("down_min"), tdt, "tile_bad"),
             (ui_label("tile_undescribed"), round(tdt - tdesc, 1), "tile_1")]
    for c, (label, val, fmt) in enumerate(tiles):
        ws.write(r0 + 2, c, label, f["tile_l"])
        ws.write(r0 + 3, c, val, f[fmt])
    ws.set_row(r0 + 2, 18)
    ws.set_row(r0 + 3, 32)

    # 2. график план/факт/% по дням; под ним таблицы
    chart_row = r0 + 5
    row = chart_row + (_rows_for(340) if counted else 0)

    ws.write(row, 0, ui_label("card_by_days"), f["section"])
    t_days = row + 1
    _write_days(ws, f, days, True, r0=t_days)
    if counted:
        # данные графика (только дни с выпуском) на скрытом листе: дни без выпуска на графике не рисуются
        cd = wb.add_worksheet(days_sheet)
        _write_days(cd, f, counted, False)
        cd.hide()
        ws.insert_chart(chart_row, 0, _chart_plan_fact(wb, days_sheet, len(counted)))
    if n:
        t = t_days + n + 1
        first, last = t_days + 2, t

        def total_of(col: str) -> str:
            """SUM только по строкам дней с выпуском (диапазонами подряд): дни без выпуска в итог не входят."""
            parts, start = [], None
            for i, d in enumerate(days + [None]):
                ok = d is not None and not d.get("no_output")
                if ok and start is None:
                    start = i
                elif not ok and start is not None:
                    a, b = first + start, first + i - 1
                    parts.append(f"{col}{a}:{col}{b}" if b > a else f"{col}{a}")
                    start = None
            return "=SUM(" + ",".join(parts) + ")" if parts else "=0"

        ws.write(t, 0, "Итого", f["bold"])
        ws.write_formula(t, 1, total_of("B"), f["bold"], tp)
        ws.write_formula(t, 2, total_of("C"), f["bold"], tf)
        ws.write_formula(t, 3, f"=C{t + 1}-B{t + 1}", f["bold"], tf - tp)
        ws.write_formula(t, 4, f'=IF(B{t + 1}=0,"",C{t + 1}/B{t + 1})', f["bpct"], (tf / tp) if tp else "")
        ws.write_formula(t, 5, f"=SUM(F{first}:F{last})", f["bn1"], tdt)
        ws.write_formula(t, 6, f"=SUM(G{first}:G{last})", f["bn1"], tdesc)
        ws.write_formula(t, 7, f"=SUM(H{first}:H{last})", f["bn1"], round(tdt - tdesc, 1))
        ws.insert_chart(t_days, 9, _chart_downtime(wb, sheet, n, r0=t_days))
    row = t_days + max(n + 3, _rows_for(300)) + 1

    ws.write(row, 0, ui_label("card_reasons"), f["section"])
    t_reasons = row + 1
    _write_reasons(ws, f, reason_rows, r0=t_reasons)
    if reason_rows:
        ws.insert_chart(t_reasons, 9, _chart_pareto(wb, sheet, min(len(reason_rows), 10), r0=t_reasons))
    row = t_reasons + max(len(reason_rows) + 3, _rows_for(340)) + 1

    ws.write(row, 0, ui_label("table_down_stations"), f["section"])
    t_st = row + 1
    _write_stations(ws, f, station_rows, r0=t_st)
    if station_rows:
        ws.insert_chart(t_st, 9, _chart_stations(wb, sheet, min(len(station_rows), 15), r0=t_st))
    end = t_st + max(len(station_rows) + 3, _rows_for(420))
    if cum:
        # 5-й график экрана: данные на скрытом листе, диаграмма под таблицей станций
        cs = wb.add_worksheet(cum_sheet)
        _write_cumulative(cs, f, cum)
        cs.hide()
        ws.insert_chart(end + 1, 0, _chart_cumulative(wb, cum_sheet, len(cum)))
        end += 1 + _rows_for(340)
    if n:
        # блок «По ответственным» (как на экране «Отчёты»): минуты по отделам из той же функции, что Andon и «Простои»
        end = write_responsibility(wb, ws, end + 1, owner_split(data))
    return end


def write_downtime_list(ls, f: dict[str, Any], data: dict[str, Any]) -> None:
    """Полный список простоев на отдельном листе (на экране его нет)."""
    items, areas, stations, reasons, starts = data["items"], data["areas"], data["stations"], data["reasons"], data["starts"]
    heads = ["Дата", "№", "Начало", "Окончание", "Участок", "Станция", "Причина", "Отв.", "Минут", "Описание", "Внёс"]
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
        ls.write(r, 7, owner_label(it["owner"], owner_status(it["owner"], it.get("answer"))))
        ls.write(r, 8, float(it["minutes"]), f["n1"])
        ls.write(r, 9, it["note"] or "")
        ls.write(r, 10, it["created_by_name"] or "")
    for c, w in enumerate([12, 5, 9, 11, 20, 20, 26, 8, 8, 40, 24]):
        ls.set_column(c, c, w)
    ls.freeze_panes(1, 0)
    ls.autofilter(0, 0, max(len(items), 1), len(heads) - 1)


def build_period_workbook(line: str, d1: date, d2: date, data: dict[str, Any], cum: list[dict[str, Any]] | None = None) -> bytes:
    """Книга по виду как вкладка «Отчёты»: плитки итогов, график, затем таблицы по дням, причинам и станциям на одном листе «Отчёт»;
    полный список простоев на листе «Простои».
    cum: интервалы дня для графика «Накопленное производство за день» (только когда период равен одному дню, как на экране)."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _fmts(wb)
    ws = wb.add_worksheet("Отчёт")
    ls = wb.add_worksheet("Простои")
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 16)
    ws.set_column(2, 7, 15)
    write_period_section(wb, ws, f, f"{line}: отчёт за период {d1.strftime('%d.%m.%Y')} — {d2.strftime('%d.%m.%Y')}", data, cum)
    write_downtime_list(ls, f, data)
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
        days = [d for d in sm["days"] if kind != "plan_fact" or not d.get("no_output")]   # на графике «план-факт» дней без выпуска нет
        n = len(days)
        if kind == "plan_fact":
            _write_days(ws, f, days, False)
            chart = _chart_plan_fact(wb, "Данные", n) if n else None
        else:
            ws.write_row(0, 0, ["Дата", ui_label("down_min"), "Описано, мин", ui_label("tile_undescribed")], f["head"])
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
    cum = await cumulative_day(pool, screen_id, d1) if d1 == d2 else None
    return build_period_workbook(line, d1, d2, data, cum), folder, f"{folder}_{d1.isoformat()}_{d2.isoformat()}.xlsx", len(data["days"])


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
    data = await _load(pool, screen_id, date_from, date_to)
    sm = summarize(data)
    return {
        "line": line, "from": date_from.isoformat(), "to": date_to.isoformat(), "responsibility": owner_split(data),
        "days": [{**d, "date": d["date"].isoformat()} for d in sm["days"]],
        "totals": sm["totals"],
        "no_output_label": dayflags.NO_OUTPUT_LABEL, "no_output_hint": dayflags.NO_OUTPUT_HINT,
        "reasons": [{"name": n, "min": m} for n, m in sm["reasons"]],
        "stations": [{"area": a, "station": s, "min": m} for a, s, m in sm["stations"]],
    }


@router.get("/charts/cumulative")
async def cumulative_data(request: Request, screen_id: int = Query(), day: date = Query(alias="date")) -> dict[str, Any]:
    """Накопленный план и факт по интервалам за один день для вкладки «Графики»; доступны всем."""
    pool: asyncpg.Pool = request.app.state.pool
    line, _ = await _screen_line(pool, screen_id)
    return {"line": line, "date": day.isoformat(), "intervals": await cumulative_day(pool, screen_id, day)}


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
    if kind == "cumulative":
        if date_from != date_to:
            raise HTTPException(status_code=400, detail="График показывается за один день")
        rows = await cumulative_day(pool, screen_id, date_from)
        if not rows:
            raise HTTPException(status_code=404, detail="За выбранный день нет данных по этой линии")
        return _xlsx_response(build_cumulative_workbook(line, date_from, rows), f"{folder}_{KIND_FILE[kind]}_{date_from.isoformat()}.xlsx", src)
    data = await _load(pool, screen_id, date_from, date_to)
    if not data["days"] and not data["items"]:
        raise HTTPException(status_code=404, detail="За выбранный период нет данных по этой линии")
    book = build_chart_workbook(kind, line, date_from, date_to, data)
    return _xlsx_response(book, f"{folder}_{KIND_FILE[kind]}_{date_from.isoformat()}_{date_to.isoformat()}.xlsx", src)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)
