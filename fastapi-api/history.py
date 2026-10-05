"""История экрана Andon по дням: почасовая таблица за выбранный день и выгрузка дня в Excel (.xlsx)."""
from __future__ import annotations

import io
import zipfile
from datetime import date
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from downtime import _screen_path, list_downtimes

router = APIRouter(prefix="/api/kpi")


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


# ---------- минимальная запись .xlsx без сторонних библиотек ----------

def _col_name(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def build_xlsx(sheet: str, headers: list[str], rows: list[list[Any]], widths: list[int]) -> bytes:
    def cell(ref: str, v: Any, style: int = 0) -> str:
        if isinstance(v, bool) or v is None or v == "":
            return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t></t></is></c>'
        if isinstance(v, (int, float)):
            return f'<c r="{ref}" s="{style}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'

    body = ['<row r="1">' + "".join(cell(f"{_col_name(i + 1)}1", h, 1) for i, h in enumerate(headers)) + "</row>"]
    for ri, row in enumerate(rows, start=2):
        body.append(f'<row r="{ri}">' + "".join(cell(f"{_col_name(i + 1)}{ri}", v) for i, v in enumerate(row)) + "</row>")
    last = f"{_col_name(len(headers))}{len(rows) + 1}"
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData>"
        f'<autoFilter ref="A1:{last}"/></worksheet>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        "</styleSheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@router.get("/screens/{screen_id}/day.xlsx")
async def day_xlsx(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date")) -> Response:
    """Выгрузка дня экрана в Excel: по строке на каждую причину простоя, интервалы без простоя одной строкой."""
    pool: asyncpg.Pool = request.app.state.pool
    scr = await pool.fetchrow("SELECT name, template, bindings FROM kpi_screens WHERE id = $1", screen_id)
    if scr is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    if day is None:
        day = await pool.fetchval("SELECT max(prod_date) FROM kpi_hourly WHERE screen_id = $1", screen_id)
        if day is None:
            raise HTTPException(status_code=404, detail="По этому экрану ещё нет данных")
    hours = await _day_rows(pool, screen_id, day)
    dts = {d["idx"]: d for d in (await list_downtimes(screen_id, request, day))["downtimes"]}
    areas = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_areas")}
    stations = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_stations")}
    reasons = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM kpi_reasons")}
    group, label = _screen_path(scr["template"], scr["name"], scr["bindings"])
    line = f"{group} › {label}"

    headers = ["Дата", "Линия", "№", "Начало", "Окончание", "Мин.", "План", "Факт", "±", "Такт-тайм за период",
               "Простой мин", "Участок", "Станция", "Причина", "Мин. по причине", "Описание", "Внёс", "Последний комментарий"]
    out: list[list[Any]] = []
    for h in hours:
        dur = (h["end_min"] or h["start_min"]) - h["start_min"]
        delta = h["fact"] - h["plan"] if (h["plan"] or h["fact"]) else ""
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
    data = build_xlsx("День", headers, out, [11, 22, 5, 9, 10, 6, 7, 7, 6, 14, 11, 16, 16, 20, 14, 36, 24, 44])
    fname = f"andon_{label.replace(' ', '_')}_{group}_{day.isoformat()}.xlsx".replace("/", "-")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
