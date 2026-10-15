"""Комбинированный отчёт автосохранения: один файл в день на линию, один видимый лист «Отчёт»:
сверху отчёт дня, ниже отчёт с начала месяца по этот день. Кнопочные выгрузки (day.xlsx, period.xlsx) этот файл не используют."""
from __future__ import annotations

import io
from datetime import date
from typing import Any

import asyncpg
import xlsxwriter

from history import DAY_HEADERS, build_day_data, cumulative_day, day_formats, day_widths, write_day_section
from repair import plc_str_of
from period import _fmts, _load, write_period_section

SHEET = "Отчёт"
GAP_ROWS = 2          # отступ между разделами
PERIOD_WIDTHS = [30, 16, 15, 15, 15, 15, 15, 15]


def build_combined_workbook(line: str, day: date, d1: date, rows: list[list[Any]], intervals: list[tuple[str, int, int]],
                            month_data: dict[str, Any], cum: list[dict[str, Any]] | None = None, headers: list[str] | None = None,
                            rest: dict[str, int | None] | None = None, plc_str: float | None = None,
                            state: str | None = None, note: str | None = None) -> bytes:
    """Книга: раздел «День <дата>» (плитки, график, таблица), под ним раздел «С начала месяца (<с> – <по>)» (плитки, графики, таблицы).
    Данные диаграмм лежат на скрытых листах; видимый лист один."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    fd, fp = day_formats(wb), _fmts(wb)
    ws = wb.add_worksheet(SHEET)
    for c, w in enumerate(day_widths(headers or DAY_HEADERS)):
        ws.set_column(c, c, max(w, PERIOD_WIDTHS[c]) if c < len(PERIOD_WIDTHS) else w)
    end = write_day_section(wb, ws, fd, f"День {day.strftime('%d.%m.%Y')} — {line}", day, rows, intervals, r0=0, chart_sheet="График", headers=headers, rest=rest, plc_str=plc_str,
                           state=state, note=note)
    write_period_section(wb, ws, fp, f"С начала месяца ({d1.strftime('%d.%m.%Y')} – {day.strftime('%d.%m.%Y')}) — {line}",
                         month_data, cum, r0=end + GAP_ROWS, cum_sheet="График накопленного", days_sheet="График дней месяца")
    wb.close()
    return buf.getvalue()


async def build_combined_report(pool: asyncpg.Pool, screen_id: int, day: date) -> tuple[bytes, str, str, int] | None:
    """(книга, имя папки линии, имя файла без префикса «Отчёт_», число интервалов дня с планом или фактом); None, если экрана нет."""
    d = await build_day_data(pool, screen_id, day)
    if d is None:
        return None
    d1 = day.replace(day=1)
    month = await _load(pool, screen_id, d1, day)
    cum = await cumulative_day(pool, screen_id, d1) if d1 == day else None
    data = build_combined_workbook(d["line"], day, d1, d["rows"], d["intervals"], month, cum, d["headers"], d["rest"], plc_str_of(d["str"]), d["state"], d["note"])
    return data, d["folder"], d["fname"], d["n"]
