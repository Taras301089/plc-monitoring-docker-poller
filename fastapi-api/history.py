"""История экрана Andon по дням: почасовая таблица за выбранный день и отчёт дня в Excel (.xlsx): один лист с итогами и графиком план/факт."""
from __future__ import annotations

import io
import json
import re
from datetime import date
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import asyncpg
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import dayflags
import labels
from labels import label as ui_label
from downtime import _screen_path, list_downtimes, owner_label
from flmodels import MODELS, fetch_counts, has_models, interval_value, model_hint
from rest_bodies import REST_FIELDS, REST_HINT, REST_TITLE, fetch_rest
from responsibility import write_responsibility
from repair import (BDC_HINT, BDC_TITLE, REPAIR_HINT, REPAIR_TITLE, STR_HINT, STR_TITLE, bdc_minutes, has_repair, plc_str_of,
                    repair_value, str_percent, str_summary)
from starve import STARVE_HINT, STARVE_TITLE, has_starve, starve_minutes

router = APIRouter(prefix="/api/kpi")

DAY_HEADERS = ["Дата", "Линия", "№", "Начало", "Окончание", "Мин.", "План", "Факт", "±", "Takt Time",
               "Отставание мин", "Отв.", "Участок", "Станция", "Причина", "Мин. по причине", "Описание", "Внёс", "Последний комментарий"]
# имя файла начинается с названия экрана, откуда его скачали (правило в CLAUDE.md)
SRC_NAMES = {"andon": "Andon", "charts": "Графики", "reports": "Отчёты", "downtimes": "Простои", "trends": "Тренды", "alarms": "Аварии", "signals": "Названия сигналов", "alarm_help": "Справка по авариям"}


def download_name(src: str, fname: str) -> str:
    return f"{SRC_NAMES.get(src, SRC_NAMES['reports'])}_{fname}"


DAY_WIDTHS = [11, 22, 5, 9, 10, 6, 7, 7, 6, 14, 11, 8, 16, 16, 20, 14, 36, 24, 44]
STARVE_WIDTH = 13
REPAIR_WIDTH = 10
BDC_WIDTH = 13


MODEL_WIDTH = 7


def day_headers(starve: bool = False, models: bool = False, repair: bool = False) -> list[str]:
    """Заголовки таблицы дня как столбцы экрана Andon: у линии Finish Line между «±» и «Takt Time» столбцы моделей (A01, P01, CS55, A13T),
    у линии с таймерами голодания после «Takt Time» ещё «Простой RB262», у FL RB294 — «Рем. зона» и «Простой BDC»."""
    h = list(DAY_HEADERS)
    h[h.index("Отв.") - 1] = ui_label("col_down_report")      # заголовок колонки из словаря; в коде колонку ищут по месту (перед «Отв.»)
    if models:
        at = h.index("±") + 1
        h[at:at] = MODELS
    if starve:
        at = h.index("Takt Time") + 1
        h.insert(at, STARVE_TITLE)
    if repair:
        at = h.index("Takt Time") + 1
        h[at:at] = [REPAIR_TITLE, BDC_TITLE]
    return h


def day_widths(headers: list[str]) -> list[int]:
    """Ширины столбцов таблицы дня под данный набор заголовков."""
    base = dict(zip(DAY_HEADERS, DAY_WIDTHS))
    base[ui_label("col_down_report")] = base["Отставание мин"]
    base.update({m: MODEL_WIDTH for m in MODELS})
    base[STARVE_TITLE] = STARVE_WIDTH
    base[REPAIR_TITLE] = REPAIR_WIDTH
    base[BDC_TITLE] = BDC_WIDTH
    return [base[h] for h in headers]


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
    # у линии бренда период без галочки «показывать» не виден нигде (кузова из него остаются в итоге дня);
    # у Finish Line такой период скрыт только пустым: план даёт ПЛК, иначе итоги разойдутся
    return await pool.fetch(
        "SELECT h.idx, h.start_min, h.end_min, h.plan, h.fact, h.takt_sec, h.starve_ms, h.repair_cnt, h.bdc_ms, h.repair_pct_plc "
        "FROM kpi_hourly h JOIN kpi_screens s ON s.id = h.screen_id "
        "LEFT JOIN kpi_schedule k ON k.screen_id = h.screen_id AND k.idx = h.idx "
        "WHERE h.screen_id = $1 AND h.prod_date = $2 "
        "AND NOT (s.template = 'body_counter' AND k.active IS FALSE) ORDER BY h.idx",
        screen_id, day,
    )


def _bindings(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


@router.get("/screens/{screen_id}/hourly")
async def hourly(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date")) -> dict[str, Any]:
    """Почасовые интервалы экрана за день (план, факт, такт) и список дней, за которые есть данные."""
    pool: asyncpg.Pool = request.app.state.pool
    scr = await pool.fetchrow("SELECT bindings FROM kpi_screens WHERE id = $1", screen_id)
    if scr is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    dates = [r["d"].isoformat() for r in await pool.fetch(
        "SELECT DISTINCT prod_date AS d FROM kpi_hourly WHERE screen_id = $1 ORDER BY d DESC LIMIT 400", screen_id)]
    if day is None:
        return {"date": None, "dates": dates, "intervals": []}
    rows = await _day_rows(pool, screen_id, day)
    st = await dayflags.day_state(pool, screen_id, day)   # закрытый день без выпуска: на экране «нет выпуска», «±» и простой не показываются
    return {
        "date": day.isoformat(), "dates": dates,
        "state": st["state"] if st else None, "no_output": bool(st and st["state"] == dayflags.NO_OUTPUT),
        "flag": st["flag"] if st else None, "note": dayflags.day_note(st),
        "no_output_label": dayflags.NO_OUTPUT_LABEL, "no_output_hint": dayflags.NO_OUTPUT_HINT,
        "intervals": [{"idx": r["idx"], "start_min": r["start_min"], "end_min": r["end_min"], "plan": r["plan"],
                       "fact": r["fact"], "takt_sec": r["takt_sec"], "starve_ms": r["starve_ms"],
                       "starve_min": starve_minutes(r["starve_ms"]), "repair_cnt": r["repair_cnt"], "bdc_ms": r["bdc_ms"],
                       "bdc_min": bdc_minutes(r["bdc_ms"])} for r in rows],
        # STR за день (та же функция, что в Excel); только у экранов со столбцом «Рем. зона»
        "str": str_summary(rows, (screen_id, day.isoformat())) if has_repair(_bindings(scr["bindings"])) else None,
    }


def day_formats(wb) -> dict[str, Any]:
    """Форматы раздела «День» (общие для отдельного отчёта дня и комбинированного файла автосохранения)."""
    tile = {"font_size": 18, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}
    return {
        "head": wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"}),
        "bold": wb.add_format({"bold": True, "top": 1}),
        "bold_n1": wb.add_format({"bold": True, "top": 1, "num_format": "0.0"}),
        "n2": wb.add_format({"num_format": "0.00"}),
        "bold_n2": wb.add_format({"bold": True, "top": 1, "num_format": "0.00"}),
        "note": wb.add_format({"italic": True, "font_color": "#6B7785"}),
        "rest_l": wb.add_format({"bold": True, "bg_color": "#F2F4F7", "border": 1, "border_color": "#D9D9D9"}),
        "str_v": wb.add_format({"bold": True, "bg_color": "#F2F4F7", "border": 1, "border_color": "#D9D9D9", "num_format": "0.00%", "align": "right"}),
        "rest_n": wb.add_format({"bold": True, "bg_color": "#F2F4F7", "border": 1, "border_color": "#D9D9D9", "num_format": "0", "align": "right"}),
        "int": wb.add_format({"num_format": "0", "align": "right"}),
        "dash_r": wb.add_format({"align": "right"}),
        "bold_int": wb.add_format({"bold": True, "top": 1, "num_format": "0", "align": "right"}),
        "bold_pct": wb.add_format({"bold": True, "num_format": "0.0%"}),
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "tile_l": wb.add_format({"font_size": 9, "bold": True, "font_color": "#6B7785", "bg_color": "#F2F4F7", "align": "center",
                                 "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}),
        "plain": wb.add_format(tile), "ok": wb.add_format({**tile, "font_color": "#1E8E5A"}),
        "tile_dim": wb.add_format({**tile, "font_size": 12, "font_color": "#6B7785"}),
        "pct": wb.add_format({**tile, "num_format": "0.0%"}), "bad": wb.add_format({**tile, "num_format": "0.0", "font_color": "#C0392B"}),
    }


def write_day_section(wb, ws, f: dict[str, Any], title: str, day: date, rows: list[list[Any]], intervals: list[tuple[str, int, int]],
                      r0: int = 0, chart_sheet: str = "График", headers: list[str] | None = None,
                      rest: dict[str, int | None] | None = None, plc_str: float | None = None,
                      state: str | None = None, note: str | None = None, resp: dict[str, Any] | None = None) -> int:
    """Раздел отчёта дня на листе ws начиная со строки r0: заголовок, подпись дня («нет выпуска» или «Простой весь день»), плитки итогов, график план/факт, таблица с итогами.
    Данные графика лежат на скрытом листе chart_sheet. rest: остаток кузовов на ФЛ по моделям (None = строки нет), строка идёт сразу под «Итого»
    и в суммы не входит. resp: итоги отставания по отделам (блок «По ответственным» под таблицей). Возвращает первую свободную строку под разделом."""
    bold, bold_n1, bold_n2, bold_pct, head = f["bold"], f["bold_n1"], f["bold_n2"], f["bold_pct"], f["head"]
    n_rows, n = len(rows), len(intervals)
    headers = headers or DAY_HEADERS
    ci = {h: i for i, h in enumerate(headers)}
    c_plan, c_fact, c_down = ci["План"], ci["Факт"], ci["Отв."] - 1      # колонка отставания стоит перед «Отв.»; её заголовок меняется в словаре
    c_starve = ci.get(STARVE_TITLE)
    c_repair, c_bdc = ci.get(REPAIR_TITLE), ci.get(BDC_TITLE)     # только у экрана FL RB294
    c_models = [ci[m] for m in MODELS if m in ci]      # столбцы моделей (только у Finish Line)

    def total(col: int) -> float:
        # «—» и пустые ячейки не числа: в Excel SUM их тоже пропускает
        return round(sum(float(r[col]) for r in rows if isinstance(r[col], (int, float))), 2 if col in (c_starve, c_bdc) else 1)

    # 1. плитки итогов (как на экране)
    ws.write(r0, 0, title, f["title"])
    if note:
        ws.write(r0 + 1, 0, note, f["note"])
    no_output = state == dayflags.NO_OUTPUT
    tp, tf = total(c_plan), total(c_fact)
    seen: set[Any] = set()
    down = 0.0
    for r in rows:
        if r[2] not in seen:
            seen.add(r[2])
            down += float(r[c_down]) if isinstance(r[c_down], (int, float)) else 0.0
    tiles = [((0, 1), ui_label("tile_plan"), tp, "plain"), ((2, 4), ui_label("tile_fact"), tf, "ok"), ((5, 7), ui_label("tile_exec"), (tf / tp) if tp else "", "pct"),
             ((8, 10), ui_label("down_min"), dayflags.NO_OUTPUT_LABEL if no_output and not down else round(down, 1),
              "tile_dim" if no_output and not down else "bad")]
    for (c1, c2), label, val, kind in tiles:
        ws.merge_range(r0 + 2, c1, r0 + 2, c2, label, f["tile_l"])
        ws.merge_range(r0 + 3, c1, r0 + 3, c2, val, f[kind])
    ws.set_row(r0 + 2, 18)
    ws.set_row(r0 + 3, 32)

    # 2. график план/факт по часам, под плитками
    chart_row = r0 + 5
    table_row = chart_row + (-(-400 // 20) + 2 if n else 0)

    # 3. таблица отчёта: шапка с фильтром, строки, итоги
    ws.write_row(table_row, 0, headers, head)
    if c_starve is not None:
        ws.write_comment(table_row, c_starve, STARVE_HINT, {"x_scale": 3, "y_scale": 1.6})
    if c_repair is not None:
        ws.write_comment(table_row, c_repair, REPAIR_HINT, {"x_scale": 2, "y_scale": 0.8})
        ws.write_comment(table_row, c_bdc, BDC_HINT, {"x_scale": 3, "y_scale": 1.2})
    for c in c_models:
        ws.write_comment(table_row, c, model_hint(headers[c]), {"x_scale": 2, "y_scale": 0.8})
    for r, row in enumerate(rows, start=table_row + 1):
        ws.write_row(r, 0, row)
        for c in c_models:
            if isinstance(row[c], (int, float)):
                ws.write_number(r, c, row[c], f["int"])
            elif row[c] == "—":
                ws.write(r, c, "—", f["dash_r"])
        if c_starve is not None and isinstance(row[c_starve], (int, float)):
            ws.write_number(r, c_starve, row[c_starve], f["n2"])
        if c_repair is not None:
            if isinstance(row[c_repair], (int, float)):
                ws.write_number(r, c_repair, row[c_repair], f["int"])
            elif row[c_repair] == "—":
                ws.write(r, c_repair, "—", f["dash_r"])
            if isinstance(row[c_bdc], (int, float)):
                ws.write_number(r, c_bdc, row[c_bdc], f["n2"])
    ws.autofilter(table_row, 0, table_row + max(n_rows, 1), len(headers) - 1)
    end = table_row + max(n_rows, 1) + 1
    if n_rows:
        # значения интервала записаны один раз, поэтому суммы столбцов верные
        t = table_row + n_rows + 1
        first, last = table_row + 2, table_row + n_rows + 1
        ws.write(t, 0, "Итого", bold)
        for c in range(1, len(headers)):
            ws.write_blank(t, c, None, bold)
        lp, lf = xlsxwriter.utility.xl_col_to_name(c_plan), xlsxwriter.utility.xl_col_to_name(c_fact)
        c_why = ci["Мин. по причине"]
        sum_cols = (ci["Мин."], c_plan, c_fact, c_down, c_why) + ((c_starve,) if c_starve is not None else ())
        for col in sum_cols:
            letter = xlsxwriter.utility.xl_col_to_name(col)
            fmt = bold_n2 if col == c_starve else bold_n1 if col in (c_down, c_why) else bold
            ws.write_formula(t, col, f"=SUM({letter}{first}:{letter}{last})", fmt, total(col))
        if c_repair is not None:   # кузова в ремонт: целое число; ни одного значения (нет связи, прошлые данные без столбца) -> «—», как у моделей
            if any(isinstance(r[c_repair], (int, float)) for r in rows):
                letter = xlsxwriter.utility.xl_col_to_name(c_repair)
                ws.write_formula(t, c_repair, f"=SUM({letter}{first}:{letter}{last})", f["bold_int"], int(total(c_repair)))
            else:
                ws.write(t, c_repair, "—", f["bold_int"])
            if any(isinstance(r[c_bdc], (int, float)) for r in rows):
                letter = xlsxwriter.utility.xl_col_to_name(c_bdc)
                ws.write_formula(t, c_bdc, f"=SUM({letter}{first}:{letter}{last})", bold_n2, total(c_bdc))
            else:
                ws.write(t, c_bdc, "—", f["bold_int"])
        if no_output:   # «±» дня без выпуска не показывается, вместо него подпись
            ws.write(t, ci["±"], dayflags.NO_OUTPUT_LABEL, bold)
        else:
            ws.write_formula(t, ci["±"], f"={lf}{t + 1}-{lp}{t + 1}", bold, tf - tp)
        for c in c_models:   # итог модели = сумма штук по интервалам; нет значений (прошлый день, нет связи) -> «—»
            if any(isinstance(r[c], (int, float)) for r in rows):
                letter = xlsxwriter.utility.xl_col_to_name(c)
                ws.write_formula(t, c, f"=SUM({letter}{first}:{letter}{last})", f["bold_int"], int(total(c)))
            else:
                ws.write(t, c, "—", f["bold_int"])
        if rest is not None:
            # текущее значение из ПЛК, а не сумма по интервалам; в SUM строка не входит
            t += 1
            if c_models:
                # подпись в начале строки, число в ячейке под столбцом своей модели (как на экране Andon), остальные ячейки пустые
                ws.merge_range(t, 0, t, min(c_models) - 1, REST_TITLE, f["rest_l"])
                ws.write_comment(t, 0, REST_HINT, {"x_scale": 3, "y_scale": 1.2})
                for c in c_models:
                    v = rest.get(headers[c])
                    if isinstance(v, int) and not isinstance(v, bool):
                        ws.write_number(t, c, v, f["rest_n"])
                    else:
                        ws.write(t, c, "—", f["rest_n"])
            else:
                # заголовки без столбцов моделей: пары «модель, число»
                ws.merge_range(t, 0, t, 4, REST_TITLE, f["rest_l"])
                ws.write_comment(t, 0, REST_HINT, {"x_scale": 3, "y_scale": 1.2})
                for k, m in enumerate(REST_FIELDS):
                    ws.write(t, 5 + 2 * k, m, f["rest_l"])
                    v = rest.get(m)
                    ws.write(t, 6 + 2 * k, v if isinstance(v, int) else "—", f["rest_n"])
            t -= 1
        if c_repair is not None:
            # STR (прямой сход): справа в строке остатка кузовов, а если её нет (прошлый день) — в такой же строке под «Итого»;
            # формула берёт факт и «Рем. зона» из строки «Итого», значение то же, что на экране
            sr = t + 1
            lp_f, lp_r = xlsxwriter.utility.xl_col_to_name(c_fact), xlsxwriter.utility.xl_col_to_name(c_repair)
            fact_ref, rep_ref = f"{lp_f}{t + 1}", f"{lp_r}{t + 1}"
            ws.write(sr, c_down, f"{STR_TITLE}:", f["rest_l"])
            ws.write_comment(sr, c_down, STR_HINT, {"x_scale": 3, "y_scale": 1.6})
            has_rep = any(isinstance(r[c_repair], (int, float)) for r in rows)
            pct = str_percent(tf, total(c_repair)) if has_rep else None
            if plc_str is not None:
                # итог дня из ПЛК (RepairPercent): число, не формула, то же значение, что на экране и в архиве
                ws.write_number(sr, c_down + 1, plc_str / 100, f["str_v"])
            else:
                ws.write_formula(sr, c_down + 1, f'=IF(OR(NOT(ISNUMBER({rep_ref})),{fact_ref}=0),"—",({fact_ref}-{rep_ref})/{fact_ref})',
                                 f["str_v"], "—" if pct is None else pct / 100)
        r_done = t + (1 if rest is not None or c_repair is not None else 0)
        ws.write(r_done + 1, 0, "Выполнение", bold_pct)
        ws.write_formula(r_done + 1, c_fact, f'=IF({lp}{t + 1}=0,"",{lf}{t + 1}/{lp}{t + 1})', bold_pct, (tf / tp) if tp else "")
        end = r_done + 2
        if c_starve is not None:
            ws.write(r_done + 2, 0, f"{STARVE_TITLE}: {STARVE_HINT}", f["note"])
            end = r_done + 3
        if c_repair is not None:
            for title, hint in ((REPAIR_TITLE, REPAIR_HINT), (BDC_TITLE, BDC_HINT), (STR_TITLE, STR_HINT)):
                ws.write(end, 0, hint if title == STR_TITLE else f"{title}: {hint}", f["note"])
                end += 1

    if resp is not None and n:
        end = write_responsibility(wb, ws, end + 1, resp)

    if n:
        cs = wb.add_worksheet(chart_sheet)
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
                "categories": [chart_sheet, 1, 0, n, 0],
                "values": [chart_sheet, 1, col, n, col],
                "fill": {"color": color},
                "data_labels": {"value": True},
                "gap": 80,
            })
        ln = wb.add_chart({"type": "line"})
        ln.add_series({"name": "% выполнения", "categories": [chart_sheet, 1, 0, n, 0], "values": [chart_sheet, 1, 3, n, 3], "y2_axis": True,
                       "line": {"color": "#27AE60", "width": 2.25}, "marker": {"type": "circle", "size": 5}})
        chart.combine(ln)
        ln.set_y2_axis({"name": "% выполнения", "num_format": "0%"})
        chart.set_title({"name": ui_label("chart_plan_fact_hour")})
        chart.set_x_axis({"name": "Интервал"})
        chart.set_y_axis({"name": "Кузовов", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 1000, "height": 400})
        ws.insert_chart(chart_row, 0, chart)
    return end


def build_day_workbook(line: str, day: date, rows: list[list[Any]], intervals: list[tuple[str, int, int]],
                       headers: list[str] | None = None, rest: dict[str, int | None] | None = None, plc_str: float | None = None,
                       state: str | None = None, note: str | None = None, resp: dict[str, Any] | None = None) -> bytes:
    """Книга Excel по виду как вкладка «Отчёты»: на листе «День» сверху плитки итогов, под ними график план/факт по часам,
    ниже таблица отчёта (с фильтром и итогами). Данные для графика лежат на скрытом листе «График»."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = day_formats(wb)
    ws = wb.add_worksheet("День")
    for c, w in enumerate(day_widths(headers or DAY_HEADERS)):
        ws.set_column(c, c, w)
    write_day_section(wb, ws, f, f"{line}, {day.strftime('%d.%m.%Y')}", day, rows, intervals, headers=headers, rest=rest, plc_str=plc_str,
                      state=state, note=note, resp=resp)
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
    await labels.refresh(pool)      # заголовки таблицы дня на экране берутся из словаря так же, как в Excel
    hours_all = await _day_rows(pool, screen_id, day)
    hours = [h for h in hours_all if h["plan"] > 0 or h["fact"] > 0]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    listed = await list_downtimes(screen_id, req, day)
    dts = {d["idx"]: d for d in listed["downtimes"]}
    areas = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_areas")}
    stations = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_stations")}
    reasons = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_reasons")}
    group, label = _screen_path(scr["template"], scr["name"], scr["bindings"])
    line = f"{group} › {label}"
    raw_b = scr["bindings"]
    bnd = json.loads(raw_b) if isinstance(raw_b, str) else dict(raw_b or {})
    starve = has_starve(bnd)
    # остаток кузовов на ФЛ: живое значение, поэтому только за сегодняшний день и только у экранов с привязками
    plc_id = await pool.fetchval("SELECT plc_id FROM kpi_screens WHERE id = $1", screen_id)
    rest_now = await fetch_rest(plc_id, bnd, day)
    models = has_models(scr["template"])
    counts = await fetch_counts(plc_id, bnd, day) if models else {}
    rep_on = has_repair(bnd)
    headers = day_headers(starve, models, rep_on)
    k = (1 if starve else 0) + (len(MODELS) if models else 0) + (2 if rep_on else 0)   # «Простой RB262», модели, «Рем. зона», «Простой BDC» сдвигают правее лежащие

    st = await dayflags.day_state(pool, screen_id, day)
    no_output = st is not None and st["state"] == dayflags.NO_OUTPUT
    out: list[list[Any]] = []
    for h in hours:
        dur = (h["end_min"] or h["start_min"]) - h["start_min"]
        delta = "" if no_output else h["fact"] - h["plan"]     # день без выпуска: «±» не показывается
        base = [day.strftime("%d.%m.%Y"), line, h["idx"], _hhmm(h["start_min"]), _hhmm(h["end_min"]), dur, h["plan"], h["fact"], delta]
        if models:
            base += [interval_value(counts, m, h["idx"]) for m in MODELS]
        base.append(_mmss(h["takt_sec"]))
        if starve:
            m = starve_minutes(h["starve_ms"])
            base.append("—" if m is None else m)
        if rep_on:
            rc = repair_value(h["repair_cnt"])
            base.append("—" if rc is None else rc)
            bm = bdc_minutes(h["bdc_ms"])
            base.append("—" if bm is None else bm)
        d = dts.get(h["idx"])
        items = (d or {}).get("items") or []
        part: list[list[Any]] = []
        if not d or (d["minutes"] <= 0 and not items):
            out.append(base + ["—" if no_output else 0, "", "—", "—", "—", 0, "", "", ""])
            continue
        for it in items:
            lc = it.get("last_comment")
            part.append(base + [d["minutes"], owner_label(it.get("owner"), it.get("owner_mark") or it.get("owner_status")), areas.get(it["area_id"], "—"), stations.get(it["station_id"], "—"),
                               reasons.get(it["reason_id"], "—"), it["minutes"], it.get("note") or "", it.get("created_by_name") or "",
                               f"{lc['text']} · {lc.get('author') or lc['user_name']}" if lc else ""])
        rest = round(d["minutes"] - d["described"], 1)
        if rest > 0.5:
            part.append(base + [d["minutes"], "", "—", "—", "не описан", rest, "", "", ""])
        # значения интервала (минуты, план, факт, ±, Takt Time, простой) пишутся один раз, на первой строке: так столбец суммируется верно
        for i, row in enumerate(part):
            if i:
                row[5:11 + k] = [""] * (6 + k)
            out.append(row)
    intervals = [(f"{_hhmm(h['start_min'])}-{_hhmm(h['end_min'] if h['end_min'] is not None else h['start_min'])}", h["plan"], h["fact"])
                 for h in hours]
    folder = safe_name(group if label in ("", "Main Line") else f"{group}_{label}")
    return {"line": line, "folder": folder, "fname": f"{folder}_{day.isoformat()}.xlsx", "rows": out, "intervals": intervals, "n": len(hours),
            "headers": headers, "rest": rest_now, "str": str_summary(hours_all, (screen_id, day.isoformat())) if rep_on else None,
            "state": st["state"] if st else None, "flag": st["flag"] if st else None, "note": dayflags.day_note(st), "no_output": no_output,
            "responsibility": listed["responsibility"]}


async def build_day_report(pool: asyncpg.Pool, screen_id: int, day: date) -> tuple[bytes, str, str, int] | None:
    """Отчёт дня по экрану: (книга, имя папки линии, имя файла, число интервалов с планом или фактом); None, если экрана нет."""
    d = await build_day_data(pool, screen_id, day)
    if d is None:
        return None
    return build_day_workbook(d["line"], day, d["rows"], d["intervals"], d["headers"], d["rest"], plc_str_of(d["str"]),
                              d["state"], d["note"], d["responsibility"]), d["folder"], d["fname"], d["n"]


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
        "line": d["line"], "date": day.isoformat(), "file": download_name("reports", d["fname"]), "headers": d["headers"], "rows": d["rows"],
        "intervals": [{"label": lbl, "plan": p, "fact": f} for lbl, p, f in d["intervals"]],
        "rest": None if d["rest"] is None else {"title": REST_TITLE, "values": d["rest"]},
        "str": d["str"],
        "state": d["state"], "flag": d["flag"], "note": d["note"], "no_output": d["no_output"], "responsibility": d["responsibility"],
        "no_output_label": dayflags.NO_OUTPUT_LABEL, "no_output_hint": dayflags.NO_OUTPUT_HINT,
    }
