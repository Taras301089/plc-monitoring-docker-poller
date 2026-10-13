"""Журнал аварий, шаг 1: словарь аварий (бит слова ПЛК -> текст аварии), импорт из экспорта TIA Portal (.xlsx)."""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from auth import require_admin, require_user

router = APIRouter(prefix="/api/alarms")

MAX_UPLOAD = 20 * 1024 * 1024
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PREFIX = re.compile(r"^(\d+)_")
UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
MAX_PERIOD_DAYS = 93
MAX_EXCEL_ROWS = 50000
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def ensure_alarms_schema(pool: asyncpg.Pool) -> None:
    # Словарь аварий: одна строка на бит (общий номер бита 0… в массиве слов UInt16, например MSG.W[n])
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_catalog (
            plc_id         INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
            bit_index      INTEGER     NOT NULL,
            word_index     INTEGER     NOT NULL,
            bit_in_word    SMALLINT    NOT NULL,
            alarm_name     TEXT        NOT NULL DEFAULT '',
            code           TEXT        NOT NULL DEFAULT '',
            text           TEXT        NOT NULL DEFAULT '',
            class          TEXT        NOT NULL DEFAULT '',
            trigger_tag    TEXT        NOT NULL DEFAULT '',
            is_placeholder BOOLEAN     NOT NULL DEFAULT FALSE,
            imported_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (plc_id, bit_index)
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_alarm_catalog_plc_word ON alarm_catalog (plc_id, word_index)")
    # Станция и устройство, распознанные из текста аварии (parse_station_device); parsed: строка уже разобрана
    await pool.execute("ALTER TABLE alarm_catalog ADD COLUMN IF NOT EXISTS station TEXT NOT NULL DEFAULT ''")
    await pool.execute("ALTER TABLE alarm_catalog ADD COLUMN IF NOT EXISTS device TEXT NOT NULL DEFAULT ''")
    await pool.execute("ALTER TABLE alarm_catalog ADD COLUMN IF NOT EXISTS parsed BOOLEAN NOT NULL DEFAULT FALSE")
    # строки, добавленные до появления колонок: разбираются один раз (потом parsed = TRUE, повторов нет)
    todo = await pool.fetch("SELECT plc_id, bit_index, text FROM alarm_catalog WHERE NOT parsed")
    if todo:
        recs = [(*parse_station_device(r["text"]), r["plc_id"], r["bit_index"]) for r in todo]
        await pool.executemany(
            "UPDATE alarm_catalog SET station = $1, device = $2, parsed = TRUE WHERE plc_id = $3 AND bit_index = $4", recs
        )
    # События аварий: одна строка на одно появление бита; пишет сборщик аварий поллера только при смене состояния бита
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_events (
            id              BIGSERIAL   PRIMARY KEY,
            plc_id          INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
            bit_index       INTEGER     NOT NULL,
            started_at      TIMESTAMPTZ NOT NULL,
            ended_at        TIMESTAMPTZ,
            active_at_start BOOLEAN     NOT NULL DEFAULT FALSE   -- была активна, когда сборщик её впервые увидел
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_alarm_events_plc_time ON alarm_events (plc_id, started_at DESC)")
    # одна открытая (не снятая) запись на бит
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alarm_events_open ON alarm_events (plc_id, bit_index) WHERE ended_at IS NULL")
    # Откуда читать аварии: узел массива слов UInt16 и их число (настройка по ПЛК)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_sources (
            plc_id     INTEGER PRIMARY KEY REFERENCES plcs(id) ON DELETE CASCADE,
            node_id    TEXT    NOT NULL,
            word_count INTEGER NOT NULL CHECK (word_count BETWEEN 1 AND 5000),
            enabled    BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )


# ------------------------------------------------------------------ чистые функции
def bit_address(bit_index: int) -> tuple[int, int]:
    """Общий номер бита -> (номер слова, бит в слове): бит 16 это W[1].x0."""
    return bit_index // 16, bit_index % 16


def normalize_text(raw: str | None) -> tuple[str, str]:
    """Текст из Excel -> (код, текст без префикса «N_»). Нестандартные пробелы заменяются обычными, края обрезаются."""
    s = re.sub(r"\s+", " ", (raw or "").replace("​", "").replace("﻿", "")).strip()
    m = _PREFIX.match(s)
    if m:
        return m.group(1), s[m.end():].strip()
    return "", s


_STATION = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,3})(\d{2,3})")
_PROFINET = re.compile(r"^Device profinet\s*\S?\s*Equipment ID\)\s*\d+\s*(?:is lost|Loss of|lost|missing|error)?\s*(\S*)", re.I)
_AREA_ONLY = re.compile(r"^(?:UB|MB)_(?=[A-Za-z])")


def _norm_station(letters: str, digits: str) -> str:
    """UB040 -> UB40: ведущий ноль в трёхзначном номере отбрасывается (в тексте встречаются обе записи одной станции)."""
    return letters + (str(int(digits)) if len(digits) == 3 and digits.startswith("0") else digits)


def parse_station_device(text: str | None) -> tuple[str, str]:
    """Текст аварии -> (станция, устройство).
    Станция: первый код вида «2-3 заглавные буквы + 2-3 цифры» (UB20, MB110, UB040 -> UB40), в любом месте текста, в том числе вплотную
    к другим символам (MB110L-3D, MB100/left). Устройство есть только у сообщений «Device profinet (Equipment ID)N lost <имя>»:
    это имя без кода станции и соседнего «_» (UB10_HMI1 -> HMI1, VDP1_UB20_VFD1_Lift -> VDP1_VFD1_Lift, UB_PLC -> PLC, станция пуста).
    У остальных аварий устройство пустое."""
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s:
        return "", ""
    m = _STATION.search(s)
    station = _norm_station(m.group(1), m.group(2)) if m else ""
    device = ""
    pm = _PROFINET.match(s)
    if pm and pm.group(1):
        tail = pm.group(1)
        sm = _STATION.search(tail)
        if sm:
            device = (tail[:sm.start()].rstrip("_") + "_" + tail[sm.end():].lstrip("_")).strip("_") if tail[:sm.start()].rstrip("_") else tail[sm.end():].lstrip("_")
        else:
            device = _AREA_ONLY.sub("", tail)
    return station, device


def _col(ref: str) -> str:
    return re.sub(r"\d+", "", ref or "")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out: list[str] = []
    with z.open("xl/sharedStrings.xml") as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag == NS + "si":
                out.append("".join(t.text or "" for t in el.iter(NS + "t")))
                el.clear()
    return out


def _sheet_name(z: zipfile.ZipFile) -> str:
    names = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    if not names:
        raise ValueError("В файле нет листа с данными")
    return names[0]


def parse_alarm_xlsx(data: bytes) -> list[dict[str, Any]]:
    """Разбор экспорта аварий. Возвращает записи по возрастанию bit_index; повтор бита: побеждает последняя строка.
    Ошибка формата: ValueError с русским текстом."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Файл не является Excel (.xlsx): не удалось открыть архив")
    rows: dict[int, dict[str, Any]] = {}
    try:
        sst = _shared_strings(z)
        sheet = _sheet_name(z)
        with z.open(sheet) as f:
            for _ev, row in ET.iterparse(f, events=("end",)):
                if row.tag != NS + "row":
                    continue
                cells: dict[str, str] = {}
                for c in row.findall(NS + "c"):
                    t = c.get("t")
                    if t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(NS + "t"))
                    else:
                        v = c.find(NS + "v")
                        if v is None or v.text is None:
                            continue
                        val = sst[int(v.text)] if t == "s" else v.text
                    cells[_col(c.get("r", ""))] = val
                row.clear()
                g = cells.get("G", "").strip()
                if not re.fullmatch(r"\d+(\.0+)?", g):
                    continue   # шапка и строки без номера бита
                bit = int(float(g))
                code, text = normalize_text(cells.get("C"))
                w, b = bit_address(bit)
                rows[bit] = {
                    "bit_index": bit,
                    "word_index": w,
                    "bit_in_word": b,
                    "alarm_name": (cells.get("B") or "").strip(),
                    "code": code,
                    "text": text,
                    "class": (cells.get("E") or "").strip(),
                    "trigger_tag": (cells.get("F") or "").strip(),
                    "is_placeholder": text == "",
                }
    except (zipfile.BadZipFile, ET.ParseError, IndexError, KeyError) as e:
        raise ValueError(f"Не удалось разобрать файл аварий ({type(e).__name__}); нужен экспорт TIA Portal «DiscreteAlarms» в .xlsx")
    if not rows:
        raise ValueError("В файле нет аварий: не найдена колонка G «Trigger bit» с номерами битов")
    return [rows[k] for k in sorted(rows)]


# ------------------------------------------------------------------ API
@router.post("/catalog/import", summary="Загрузить словарь аварий ПЛК из .xlsx (только администратор)")
async def import_catalog(request: Request, plc_id: int = Query(...), user: dict = Depends(require_admin)):
    pool: asyncpg.Pool = request.app.state.pool
    if not await pool.fetchval("SELECT 1 FROM plcs WHERE id = $1", plc_id):
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 20 МБ")
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 20 МБ")
    try:
        items = await run_in_threadpool(parse_alarm_xlsx, data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    records = [
        (plc_id, i["bit_index"], i["word_index"], i["bit_in_word"], i["alarm_name"], i["code"], i["text"],
         i["class"], i["trigger_tag"], i["is_placeholder"], *parse_station_device(i["text"]))
        for i in items
    ]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM alarm_catalog WHERE plc_id = $1", plc_id)
            await conn.executemany(
                "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, alarm_name, code, text, class, trigger_tag, is_placeholder, station, device, parsed) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, TRUE)",
                records,
            )
    return {"imported": len(items), "placeholders": sum(1 for i in items if i["is_placeholder"]), "plc_id": plc_id}


@router.get("/catalog", summary="Страница словаря аварий ПЛК")
async def get_catalog(
    request: Request,
    plc_id: int = Query(...),
    q: str = Query("", max_length=200),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_placeholders: bool = False,
    user: dict = Depends(require_user),
):
    pool: asyncpg.Pool = request.app.state.pool
    q = q.strip()
    like = None
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
    cond = "plc_id = $1 AND ($2::text IS NULL OR text ILIKE $2 OR code = $3 OR alarm_name ILIKE $2)"
    where = cond + ("" if include_placeholders else " AND NOT is_placeholder")
    counts = await pool.fetchrow(
        "SELECT count(*) AS n, count(*) FILTER (WHERE is_placeholder) AS ph FROM alarm_catalog WHERE plc_id = $1", plc_id
    )
    total = await pool.fetchval(f"SELECT count(*) FROM alarm_catalog WHERE {where}", plc_id, like, q)
    rows = await pool.fetch(
        f"SELECT bit_index, word_index, bit_in_word, alarm_name, code, text, class, trigger_tag, is_placeholder "
        f"FROM alarm_catalog WHERE {where} ORDER BY bit_index LIMIT $4 OFFSET $5",
        plc_id, like, q, limit, offset,
    )
    return {
        "plc_id": plc_id,
        "total": total,
        "all": counts["n"],
        "placeholders": counts["ph"],
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


# ------------------------------------------------------------------ журнал событий
def plant_day_start_utc(d: date) -> datetime:
    """Начало плантовых суток d (00:00 по KPI_UTC_OFFSET_HOURS) в UTC."""
    return datetime.combine(d, time.min, tzinfo=timezone.utc) - timedelta(hours=UTC_OFFSET_HOURS)


def to_plant(dt: datetime) -> datetime:
    """UTC -> плантовое время (без часового пояса, для Excel)."""
    return (dt.astimezone(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).replace(tzinfo=None, microsecond=0)


def plant_today() -> date:
    return to_plant(datetime.now(timezone.utc)).date()


def format_duration(sec: int | None) -> str:
    """Длительность текстом как на экране: «26 ч 20 мин 53 с», «5 мин 3 с», «12 с»."""
    sec = max(0, int(sec or 0))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} ч {m} мин {s} с"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def event_item(r: Any, now: datetime) -> dict[str, Any]:
    """Одна запись журнала в виде для API и Excel (единственное место, где считаются отметки времени и длительность)."""
    started, ended = r["started_at"], r["ended_at"]
    dur = max(0, int(((ended or now) - started).total_seconds()))
    in_catalog = bool(r["cat"]) and (r["text"] or "") != ""
    return {
        "id": r["id"],
        "bit_index": r["bit_index"],
        "code": r["code"] or "",
        "text": r["text"] if in_catalog else f"Бит {r['bit_index']} (нет в словаре)",
        "class": r["class"] or "",
        "station": r["station"] or "",
        "device": r["device"] or "",
        "source": "HMI",
        "started_at": _iso(started),
        "ended_at": _iso(ended) if ended else None,
        "duration_sec": dur,
        "duration_text": format_duration(dur),
        "active": ended is None,
        "active_at_start": bool(r["active_at_start"]),
    }


_SORTS = {
    "started_at": "e.started_at",
    "ended_at": "e.ended_at",
    "duration": "(COALESCE(e.ended_at, $2::timestamptz) - e.started_at)",
    "code": "(CASE WHEN c.code ~ '^[0-9]{1,15}$' THEN c.code::bigint END)",
    "station": "c.station",
}


def _check_period(d1: date | None, d2: date | None) -> None:
    if d1 and d2:
        if d2 < d1:
            raise HTTPException(status_code=400, detail="Дата «По» раньше даты «С»")
        if (d2 - d1).days + 1 > MAX_PERIOD_DAYS:
            raise HTTPException(status_code=400, detail=f"Период не более {MAX_PERIOD_DAYS} дней")


async def _plc_info(pool: asyncpg.Pool, plc_id: int) -> dict[str, Any]:
    from main import plc_link_state   # позже, чтобы не было круговой зависимости при старте

    r = await pool.fetchrow(
        """
        SELECT p.id, p.name, p.opc_endpoint, p.is_active, s.is_connected,
               extract(epoch FROM (now() - s.updated_at))::int AS status_age
        FROM plcs p LEFT JOIN plc_connection_status s ON s.plc_id = p.id WHERE p.id = $1
        """,
        plc_id,
    )
    if r is None:
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    m = re.search(r"://([^:/]+)", r["opc_endpoint"] or "")
    return {
        "plc": {"id": r["id"], "name": r["name"], "ip": m.group(1) if m else "N/A"},
        "link_state": plc_link_state(r["is_active"], r["is_connected"], r["status_age"]),
    }


async def fetch_journal(
    pool: asyncpg.Pool, plc_id: int, *, state: str, station: str, device: str, cls: str, q: str,
    d1: date | None, d2: date | None, sort: str, direction: str, limit: int, offset: int,
) -> dict[str, Any]:
    """Журнал аварий ПЛК: общая функция для JSON-ответа и Excel (числа и тексты одни и те же)."""
    _check_period(d1, d2)
    info = await _plc_info(pool, plc_id)
    now = datetime.now(timezone.utc)
    args: list[Any] = [plc_id, now]
    base = ["e.plc_id = $1", "$2::timestamptz IS NOT NULL"]

    def add(cond: str, val: Any) -> None:
        args.append(val)
        base.append(cond.replace("$#", f"${len(args)}"))

    if station:
        add("c.station = $#", station)
    if device:
        add("c.device = $#", device)
    if cls:
        add("c.class = $#", cls)
    q = q.strip()
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{esc}%")
        args.append(q)
        base.append(f"(c.text ILIKE ${len(args) - 1} OR c.code = ${len(args)})")
    period: list[str] = []
    if d1:
        args.append(plant_day_start_utc(d1))
        period.append(f"e.started_at >= ${len(args)}")
    if d2:
        args.append(plant_day_start_utc(d2 + timedelta(days=1)))
        period.append(f"e.started_at < ${len(args)}")
    period_sql = " AND ".join(period) or "TRUE"
    hist = f"(e.ended_at IS NOT NULL AND {period_sql})"
    act = "e.ended_at IS NULL"
    where_state = {"active": act, "history": hist, "all": f"({act} OR {hist})"}[state]
    frm = "FROM alarm_events e LEFT JOIN alarm_catalog c ON c.plc_id = e.plc_id AND c.bit_index = e.bit_index"
    base.append(f"({hist} OR TRUE)")   # все параметры периода всегда упомянуты (иначе asyncpg не выведет их тип)
    common = " AND ".join(base)

    row = await pool.fetchrow(
        f"SELECT count(*) FILTER (WHERE {act}) AS a, count(*) FILTER (WHERE {hist}) AS h, "
        f"count(*) FILTER (WHERE {where_state}) AS t {frm} WHERE {common}",
        *args,
    )
    dirsql = "ASC" if direction == "asc" else "DESC"
    rows = await pool.fetch(
        f"SELECT e.id, e.bit_index, e.started_at, e.ended_at, e.active_at_start, c.plc_id IS NOT NULL AS cat, "
        f"c.code, c.text, c.class, c.station, c.device {frm} WHERE {common} AND {where_state} "
        f"ORDER BY {_SORTS[sort]} {dirsql} NULLS LAST, e.id {dirsql} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args, limit, offset,
    )
    return {
        **info,
        "now": _iso(now),
        "offset_hours": UTC_OFFSET_HOURS,
        "counts": {"active": row["a"], "history": row["h"]},
        "total": row["t"],
        "items": [event_item(r, now) for r in rows],
    }


def _journal_params(
    plc_id: int = Query(...),
    state: str = Query("active", pattern="^(active|history|all)$"),
    station: str = Query("", max_length=50),
    device: str = Query("", max_length=100),
    cls: str = Query("", alias="class", max_length=100),
    q: str = Query("", max_length=200),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    sort: str = Query("started_at", pattern="^(started_at|ended_at|duration|code|station)$"),
    direction: str = Query("desc", alias="dir", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    return dict(plc_id=plc_id, state=state, station=station, device=device, cls=cls, q=q, d1=date_from, d2=date_to,
                sort=sort, direction=direction)


@router.get("/events", summary="Журнал аварий ПЛК: активные и история (чтение без входа)")
async def get_events(
    request: Request,
    params: dict[str, Any] = Depends(_journal_params),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    out = await fetch_journal(request.app.state.pool, limit=limit, offset=offset, **params)
    out["limit"], out["offset"] = limit, offset
    return out


@router.get("/filters", summary="Значения для списков фильтров журнала аварий")
async def get_filters(request: Request, plc_id: int = Query(...)):
    pool: asyncpg.Pool = request.app.state.pool
    out: dict[str, list[str]] = {}
    for key, col in (("stations", "station"), ("devices", "device"), ("classes", "class")):
        rows = await pool.fetch(
            f'SELECT DISTINCT "{col}" AS v FROM alarm_catalog WHERE plc_id = $1 AND NOT is_placeholder AND "{col}" <> \'\' ORDER BY 1',
            plc_id,
        )
        out[key] = [r["v"] for r in rows]
    return out


# ------------------------------------------------------------------ Excel
XLS_HEADERS = ["Появление", "Снятие", "Станция", "Устройство", "Код", "Текст аварии", "Источник", "Класс", "Состояние",
               "Длительность", "Длительность, мин"]
XLS_WIDTHS = [19, 19, 10, 18, 8, 60, 10, 14, 12, 18, 12]
STATE_NAMES = {"active": "активные", "history": "история", "all": "все"}


def build_journal_workbook(plc_name: str, d1: date | None, d2: date | None, state: str, data: dict[str, Any]) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Журнал аварий")
    tile = {"font_size": 18, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}
    f = {
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "head": wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"}),
        "dt": wb.add_format({"num_format": "dd.mm.yyyy hh:mm:ss", "align": "left"}),
        "n1": wb.add_format({"num_format": "0.0"}),
        "bold": wb.add_format({"bold": True}),
        "tile_l": wb.add_format({"font_size": 9, "bold": True, "font_color": "#6B7785", "bg_color": "#F2F4F7", "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}),
        "tile_bad": wb.add_format({**tile, "font_color": "#C0392B"}),
        "tile_v": wb.add_format(tile),
        "bad": wb.add_format({"font_color": "#C0392B"}),
        "ok": wb.add_format({"font_color": "#1E8E5A"}),
    }
    fmt = lambda d: d.strftime("%d.%m.%Y")  # noqa: E731
    if d1 and d2:
        period = fmt(d1) if d1 == d2 else f"{fmt(d1)} – {fmt(d2)}"
    elif d1 or d2:
        period = f"с {fmt(d1)}" if d1 else f"по {fmt(d2)}"
    else:
        period = "весь период"
    ws.write(0, 0, f"Журнал аварий: {plc_name}", f["title"])
    ws.write(1, 0, f"Период: {period}. Состояние: {STATE_NAMES[state]}", f["bold"])
    ws.write(3, 0, "Активные", f["tile_l"])
    ws.write(3, 1, "История за период", f["tile_l"])
    ws.write(4, 0, data["counts"]["active"], f["tile_bad"])
    ws.write(4, 1, data["counts"]["history"], f["tile_v"])
    ws.set_row(4, 30)
    top = 6
    ws.write(top - 1, 0, "Журнал аварий", f["title"])
    for c, h in enumerate(XLS_HEADERS):
        ws.write(top, c, h, f["head"])
    ws.set_row(top, 30)
    for c, w in enumerate(XLS_WIDTHS):
        ws.set_column(c, c, w)
    r = top
    for it in data["items"]:
        r += 1
        ws.write_datetime(r, 0, to_plant(datetime.fromisoformat(it["started_at"].replace("Z", "+00:00"))), f["dt"])
        if it["ended_at"]:
            ws.write_datetime(r, 1, to_plant(datetime.fromisoformat(it["ended_at"].replace("Z", "+00:00"))), f["dt"])
        else:
            ws.write_blank(r, 1, None, f["dt"])
        ws.write(r, 2, it["station"])
        ws.write(r, 3, it["device"])
        ws.write(r, 4, it["code"])
        ws.write(r, 5, it["text"])
        ws.write(r, 6, it["source"])
        ws.write(r, 7, it["class"])
        ws.write(r, 8, "активна" if it["active"] else "снята", f["bad"] if it["active"] else f["ok"])
        ws.write(r, 9, it["duration_text"])
        ws.write_number(r, 10, round(it["duration_sec"] / 60, 1), f["n1"])
    ws.autofilter(top, 0, max(r, top + 1), len(XLS_HEADERS) - 1)
    ws.freeze_panes(top + 1, 0)
    ws.write(r + 2, 0, "Итого записей", f["bold"])
    ws.write_formula(r + 2, 1, f"=COUNTA(F{top + 2}:F{r + 1})" if r > top else "=0", f["bold"], len(data["items"]))
    wb.close()
    return buf.getvalue()


@router.get("/events.xlsx", summary="Журнал аварий в Excel (по тем же фильтрам, что на экране)")
async def get_events_xlsx(
    request: Request,
    params: dict[str, Any] = Depends(_journal_params),
    src: str = Query("alarms"),
):
    from history import download_name, safe_name

    pool: asyncpg.Pool = request.app.state.pool
    data = await fetch_journal(pool, limit=MAX_EXCEL_ROWS + 1, offset=0, **params)
    if data["total"] > MAX_EXCEL_ROWS:
        raise HTTPException(status_code=413, detail=f"В выгрузке {data['total']} записей, максимум {MAX_EXCEL_ROWS}: сузьте период или фильтры")
    d1, d2 = params["d1"], params["d2"]
    xls = await run_in_threadpool(build_journal_workbook, data["plc"]["name"], d1, d2, params["state"], data)
    if d1 and d2 and d1 != d2:
        days = f"{d1.isoformat()}_{d2.isoformat()}"
    else:
        days = (d1 or d2 or plant_today()).isoformat()
    fname = download_name(src, f"{safe_name(data['plc']['name'])}_{days}.xlsx")
    return Response(content=xls, media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


# ------------------------------------------------------------------ аналитика аварий
TOP_N = 20          # строк в таблицах «Самые частые», «Самые длительные», «Самые долгие случаи»
PARETO_N = 10       # столбцов в Парето
KPI_TEMPLATES = ("body_counter", "fl_counter")   # экраны, у которых есть почасовые данные kpi_hourly
NO_VALUE = "—"
TILE_NAMES = ["Событий", "Суммарное время аварий", "Разных аварий"]
TABLE_TITLES = {
    "frequent": "Самые частые",
    "longest": "Самые длительные",
    "cases": "Самые долгие случаи",
    "stations": "По станциям",
    "days": "По дням",
}
PARETO_TITLE = "Парето аварий (топ-10)"
PARETO_METRICS = {"count": "по числу появлений", "time": "по времени"}
SHARE_COUNT = "Доля по числу, %"
SHARE_TIME = "Доля по времени, %"
FREQ_HEADERS = ["ПЛК", "Станция", "Код", "Авария", "Появлений", "Суммарно, мин", "Среднее, мин", "Максимум, мин"]
CASE_HEADERS = ["ПЛК", "Станция", "Код", "Авария", "Появление", "Длительность, мин"]
STATION_HEADERS = ["ПЛК", "Станция", "Появлений", "Суммарно, мин", "Среднее, мин", SHARE_TIME]
DAY_HEADERS = ["Дата", "Появлений", "Суммарно, мин", "Среднее, мин"]


def tile_duration(sec: int | None) -> str:
    """Длительность в плитке как на экране (fmtDuration в alarms.js): «1 ч 5 мин», «30 мин», «0 с»; нулевые части опускаются."""
    s = max(0, int(sec or 0))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    parts = ([f"{h} ч"] if h else []) + ([f"{m} мин"] if m else []) + ([f"{x} с"] if x or not (h or m) else [])
    return " ".join(parts)


def plc_brand(name: str | None) -> str:
    """Бренд ПЛК: часть имени до первого «_» (GWM_UB -> GWM); без «_» бренд равен имени (Changan). Как plcBrand в alarms.js."""
    n = (name or "").strip()
    i = n.find("_")
    return n[:i] if i > 0 else n


def plc_area(name: str | None) -> str:
    """Участок ПЛК: часть имени после первого «_» (GWM_UB -> UB, AE_WBS_1 -> WBS 1); у имени без «_» пусто."""
    n = (name or "").strip()
    i = n.find("_")
    return n[i + 1:].replace("_", " ") if i > 0 else ""


def merge_intervals(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Объединение пересекающихся и соприкасающихся интервалов (по возрастанию начала)."""
    out: list[list[datetime]] = []
    for a, b in sorted(iv):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def started_in_work(start: datetime, iv: list[tuple[datetime, datetime]]) -> bool:
    """Начало аварии внутри рабочего интервала (граница начала включается, конца нет)."""
    from bisect import bisect_right

    i = bisect_right([a for a, _ in iv], start) - 1
    return i >= 0 and iv[i][0] <= start < iv[i][1]


def work_overlap_sec(start: datetime, end: datetime, iv: list[tuple[datetime, datetime]]) -> float:
    """Сколько секунд авария [start, end) приходится на рабочие интервалы (обрезка границами смены)."""
    from bisect import bisect_right

    total = 0.0
    i = max(0, bisect_right([a for a, _ in iv], start) - 1)
    while i < len(iv) and iv[i][0] < end:
        lo, hi = max(start, iv[i][0]), min(end, iv[i][1])
        if hi > lo:
            total += (hi - lo).total_seconds()
        i += 1
    return total


async def load_work_intervals(pool: asyncpg.Pool, plc_ids: list[int], d1: date, d2: date) -> dict[int, list[tuple[datetime, datetime]] | None]:
    """Рабочее время линий по ПЛК за период (в UTC) для правила «линия на простое в аналитику не попадает».

    ПЛК без экранов KPI (body_counter / fl_counter): None, то есть рабочим считается всё время.
    У остальных: интервалы тех производственных дней, где суммарный факт по экранам ПЛК больше нуля, только включённые
    в расписании (kpi_schedule.active), объединение интервалов всех экранов ПЛК. Минуты интервала отсчитываются от
    начала производственного дня (могут быть больше 1440: интервалы после полуночи)."""
    with_screen = {r["plc_id"] for r in await pool.fetch(
        "SELECT DISTINCT plc_id FROM kpi_screens WHERE plc_id = ANY($1::int[]) AND template = ANY($2::text[])", plc_ids, list(KPI_TEMPLATES))}
    rows = await pool.fetch(
        """
        SELECT s.plc_id, h.prod_date, h.start_min, COALESCE(h.end_min, k.end_min) AS end_min,
               COALESCE(k.active, TRUE) AS active, h.fact
        FROM kpi_hourly h
        JOIN kpi_screens s ON s.id = h.screen_id
        LEFT JOIN kpi_schedule k ON k.screen_id = h.screen_id AND k.idx = h.idx
        WHERE s.plc_id = ANY($1::int[]) AND s.template = ANY($4::text[]) AND h.prod_date BETWEEN $2 AND $3
        """,
        plc_ids, d1 - timedelta(days=2), d2 + timedelta(days=1), list(KPI_TEMPLATES),
    )
    fact: dict[tuple[int, date], int] = {}
    for r in rows:
        k = (r["plc_id"], r["prod_date"])
        fact[k] = fact.get(k, 0) + int(r["fact"] or 0)
    raw: dict[int, list[tuple[datetime, datetime]]] = {p: [] for p in with_screen}
    for r in rows:
        if not r["active"] or fact[(r["plc_id"], r["prod_date"])] <= 0 or r["end_min"] is None or r["end_min"] <= r["start_min"]:
            continue
        base = plant_day_start_utc(r["prod_date"])
        raw[r["plc_id"]].append((base + timedelta(minutes=r["start_min"]), base + timedelta(minutes=r["end_min"])))
    return {p: (merge_intervals(raw[p]) if p in with_screen else None) for p in plc_ids}


def build_analytics(
    events: list[dict[str, Any]], work: dict[int, list[tuple[datetime, datetime]] | None] | None, now: datetime, include_all: bool,
) -> dict[str, Any]:
    """Единственное место расчёта аналитики аварий (экран и Excel берут результат отсюда).

    events: plc_id, plc, bit_index, started_at, ended_at, code, text, station, in_catalog.
    work: рабочие интервалы по ПЛК (None у ПЛК без экрана KPI: рабочее всё время); не используется при include_all.
    Авария учитывается, если НАЧАЛАСЬ в рабочее время; её длительность обрезается границами рабочих интервалов."""
    by_alarm: dict[tuple[int, int], dict[str, Any]] = {}
    by_station: dict[tuple[int, str], dict[str, Any]] = {}
    by_day: dict[date, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    excluded = 0
    for ev in events:
        s = ev["started_at"]
        e = max(ev["ended_at"] or now, s)
        iv = None if include_all or work is None else work.get(ev["plc_id"])
        if iv is None:
            dur = (e - s).total_seconds()
        elif started_in_work(s, iv):
            dur = work_overlap_sec(s, e, iv)
        else:
            excluded += 1
            continue
        text = ev["text"] if ev["in_catalog"] and ev["text"] else f"Бит {ev['bit_index']} (нет в словаре)"
        station = ev["station"] or NO_VALUE
        code = ev["code"] or NO_VALUE
        a = by_alarm.setdefault((ev["plc_id"], ev["bit_index"]), {
            "plc_id": ev["plc_id"], "plc": ev["plc"], "station": station, "code": code, "text": text,
            "bit_index": ev["bit_index"], "count": 0, "total_sec": 0.0, "max_sec": 0.0})
        a["count"] += 1
        a["total_sec"] += dur
        a["max_sec"] = max(a["max_sec"], dur)
        st = by_station.setdefault((ev["plc_id"], station), {"plc_id": ev["plc_id"], "plc": ev["plc"], "station": station,
                                                              "count": 0, "total_sec": 0.0, "keys": set()})
        st["count"] += 1
        st["total_sec"] += dur
        st["keys"].add(ev["bit_index"])
        day = to_plant(s).date()
        dd = by_day.setdefault(day, {"date": day, "count": 0, "total_sec": 0.0})
        dd["count"] += 1
        dd["total_sec"] += dur
        cases.append({"plc": ev["plc"], "station": station, "code": code, "text": text, "started_at": _iso(s),
                      "duration_sec": int(round(dur)), "active": ev["ended_at"] is None})

    n_events = sum(a["count"] for a in by_alarm.values())
    total_sec = sum(a["total_sec"] for a in by_alarm.values())

    def share(x: float, tot: float) -> float:
        return round(x / tot, 6) if tot else 0.0

    alarms = []
    for a in by_alarm.values():
        alarms.append({**a, "total_sec": int(round(a["total_sec"])), "max_sec": int(round(a["max_sec"])),
                       "avg_sec": round(a["total_sec"] / a["count"], 1),
                       "share_count": share(a["count"], n_events), "share_time": share(a["total_sec"], total_sec)})
    frequent = sorted(alarms, key=lambda a: (-a["count"], -a["total_sec"], a["text"]))[:TOP_N]
    longest = sorted(alarms, key=lambda a: (-a["total_sec"], -a["count"], a["text"]))[:TOP_N]
    cases.sort(key=lambda c: (-c["duration_sec"], c["started_at"]))
    stations = sorted(
        ({"plc_id": s["plc_id"], "plc": s["plc"], "station": s["station"], "count": s["count"], "total_sec": int(round(s["total_sec"])),
          "avg_sec": round(s["total_sec"] / s["count"], 1), "distinct": len(s["keys"]), "share_time": share(s["total_sec"], total_sec)}
         for s in by_station.values()),
        key=lambda s: (-s["total_sec"], -s["count"], s["plc"], s["station"]))
    days = [{"date": d.isoformat(), "count": v["count"], "total_sec": int(round(v["total_sec"])),
             "avg_sec": round(v["total_sec"] / v["count"], 1)} for d, v in sorted(by_day.items())]

    def pareto(metric: str) -> list[dict[str, Any]]:
        key = (lambda a: a["count"]) if metric == "count" else (lambda a: a["total_sec"])
        tot = sum(key(a) for a in alarms)
        out, cum = [], 0
        for a in sorted(alarms, key=lambda a: (-key(a), a["text"]))[:PARETO_N]:
            cum += key(a)
            out.append({"label": a["text"], "plc": a["plc"], "station": a["station"], "code": a["code"], "count": a["count"],
                        "total_sec": a["total_sec"], "value": a["count"] if metric == "count" else round(a["total_sec"] / 60, 1),
                        "share": share(key(a), tot), "cum": share(cum, tot)})
        return out

    return {
        "tiles": {"events": n_events, "total_sec": int(round(total_sec)), "distinct": len(by_alarm)},
        "excluded": excluded,
        "include_all": include_all,
        "frequent": frequent,
        "longest": longest,
        "cases": cases[:TOP_N],
        "stations": stations,
        "days": days,
        "pareto": {"count": pareto("count"), "time": pareto("time")},
    }


async def fetch_analytics(pool: asyncpg.Pool, d1: date, d2: date, brand: str, plc_id: int | None, include_all: bool) -> dict[str, Any]:
    """Аналитика аварий за период для выбранных ПЛК: «все» (brand и plc_id пусты), бренд или один ПЛК."""
    _check_period(d1, d2)
    plcs = await pool.fetch("SELECT id, name FROM plcs ORDER BY sort_order NULLS LAST, id")
    if plc_id is not None:
        scope = [p for p in plcs if p["id"] == plc_id]
        if not scope:
            raise HTTPException(status_code=404, detail="ПЛК не найден")
    elif brand:
        scope = [p for p in plcs if plc_brand(p["name"]).lower() == brand.lower()]
    else:
        scope = list(plcs)
    ids = [p["id"] for p in scope]
    names = {p["id"]: p["name"] for p in scope}
    label = scope[0]["name"] if plc_id is not None else (brand or "все")
    now = datetime.now(timezone.utc)
    events: list[dict[str, Any]] = []
    with_data = 0
    work = None
    if ids:
        rows = await pool.fetch(
            """
            SELECT e.plc_id, e.bit_index, e.started_at, e.ended_at, c.plc_id IS NOT NULL AS cat, c.code, c.text, c.station
            FROM alarm_events e LEFT JOIN alarm_catalog c ON c.plc_id = e.plc_id AND c.bit_index = e.bit_index
            WHERE e.plc_id = ANY($1::int[]) AND e.started_at >= $2 AND e.started_at < $3
            ORDER BY e.started_at, e.id
            """,
            ids, plant_day_start_utc(d1), plant_day_start_utc(d2 + timedelta(days=1)),
        )
        events = [{"plc_id": r["plc_id"], "plc": names[r["plc_id"]], "bit_index": r["bit_index"], "started_at": r["started_at"],
                   "ended_at": r["ended_at"], "code": r["code"] or "", "text": r["text"] or "", "station": r["station"] or "",
                   "in_catalog": bool(r["cat"])} for r in rows]
        with_data = await pool.fetchval(
            "SELECT count(*) FROM (SELECT plc_id FROM alarm_catalog WHERE plc_id = ANY($1::int[]) UNION "
            "SELECT plc_id FROM alarm_events WHERE plc_id = ANY($1::int[])) x", ids)
        if not include_all:
            work = await load_work_intervals(pool, ids, d1, d2)
    out = build_analytics(events, work, now, include_all)
    out.update({
        "from": d1.isoformat(), "to": d2.isoformat(), "now": _iso(now), "offset_hours": UTC_OFFSET_HOURS,
        "scope": {"brand": brand, "plc_id": plc_id, "label": label, "plcs": [{"id": p["id"], "name": p["name"]} for p in scope],
                  "plcs_with_data": with_data},
    })
    return out


def _analytics_params(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    brand: str = Query("", max_length=50),
    plc_id: int | None = Query(None),
    all_hours: bool = Query(False, description="Учитывать аварии вне смены и в дни без выпуска"),
) -> dict[str, Any]:
    t = plant_today()
    d1, d2 = date_from or date_to or t, date_to or date_from or t
    return dict(d1=d1, d2=d2, brand=brand.strip(), plc_id=plc_id, include_all=all_hours)


@router.get("/analytics", summary="Аналитика аварий за период: топы, Парето, по станциям и дням (чтение без входа)")
async def get_analytics(request: Request, params: dict[str, Any] = Depends(_analytics_params)):
    return await fetch_analytics(request.app.state.pool, **params)


def build_analytics_workbook(data: dict[str, Any], metric: str) -> bytes:
    """Excel аналитики: один лист «Аналитика» повторяет экран (заголовок, плитки, график Парето, таблицы); данные графика на скрытом листе «График»."""
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Аналитика")
    gs = wb.add_worksheet("График")
    gs.hide()
    tile = {"font_size": 18, "bold": True, "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}
    f = {
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "bold": wb.add_format({"bold": True}),
        "muted": wb.add_format({"font_color": "#6B7785"}),
        "head": wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"}),
        "head_r": wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter", "align": "right"}),
        "tile_l": wb.add_format({"font_size": 9, "bold": True, "font_color": "#6B7785", "bg_color": "#F2F4F7", "align": "center", "valign": "vcenter", "border": 1, "border_color": "#D9D9D9"}),
        "tile_v": wb.add_format(tile),
        "tile_bad": wb.add_format({**tile, "font_color": "#C0392B"}),
        "n1": wb.add_format({"num_format": "0.0"}),
        "n1b": wb.add_format({"num_format": "0.0", "bold": True, "top": 1}),
        "int": wb.add_format({"num_format": "0"}),
        "intb": wb.add_format({"num_format": "0", "bold": True, "top": 1}),
        "pct": wb.add_format({"num_format": "0.0%"}),
        "pctb": wb.add_format({"num_format": "0.0%", "bold": True, "top": 1}),
        "dt": wb.add_format({"num_format": "dd.mm.yyyy hh:mm:ss", "align": "left"}),
        "d": wb.add_format({"num_format": "dd.mm.yyyy", "align": "left"}),
        "totl": wb.add_format({"bold": True, "top": 1}),
        "bad": wb.add_format({"font_color": "#C0392B"}),
    }
    for c, w in enumerate([14, 10, 11, 52, 16, 16, 16, 16]):
        ws.set_column(c, c, w)
    d1, d2 = date.fromisoformat(data["from"]), date.fromisoformat(data["to"])
    fmt = lambda d: d.strftime("%d.%m.%Y")  # noqa: E731
    period = fmt(d1) if d1 == d2 else f"{fmt(d1)} – {fmt(d2)}"
    label = data["scope"]["label"]
    ws.write(0, 0, f"Аналитика аварий: {'все линии' if label == 'все' else label}", f["title"])
    ws.write(1, 0, f"Период: {period}", f["bold"])
    if data["include_all"]:
        ws.write(2, 0, "Учитываются все аварии, в том числе вне смены и в дни без выпуска", f["muted"])
    else:
        ws.write(2, 0, f"Учитываются аварии, начавшиеся в рабочее время линии. Не учтено вне смены и в дни без выпуска: {data['excluded']}", f["muted"])
    t = data["tiles"]
    for c, name in enumerate(TILE_NAMES):
        ws.write(4, c, name, f["tile_l"])
    ws.write(5, 0, t["events"], f["tile_v"])
    ws.write(5, 1, tile_duration(t["total_sec"]), f["tile_bad"])
    ws.write(5, 2, t["distinct"], f["tile_v"])
    ws.set_row(5, 30)
    ws.set_row(4, 30)

    # график Парето: данные на скрытом листе «График»
    par = data["pareto"][metric]
    gs.write_row(0, 0, ["Авария", "Число появлений" if metric == "count" else "Суммарно, мин", "Накопленная доля"])
    for i, p in enumerate(par, start=1):
        gs.write(i, 0, p["label"])
        gs.write_number(i, 1, p["value"])
        gs.write_number(i, 2, p["cum"])
    row = 7
    if par:
        n = len(par)
        cats = ["График", 1, 0, n, 0]
        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "Число появлений" if metric == "count" else "Суммарно, мин", "categories": cats,
                       "values": ["График", 1, 1, n, 1], "fill": {"color": "#2F80ED"}, "gap": 50})
        ln = wb.add_chart({"type": "line"})
        ln.add_series({"name": "Накопленная доля", "categories": cats, "values": ["График", 1, 2, n, 2], "y2_axis": True,
                       "line": {"color": "#27AE60", "width": 2}, "marker": {"type": "circle", "size": 5}})
        ch.combine(ln)
        ch.set_title({"name": f"{PARETO_TITLE}, {PARETO_METRICS[metric]}"})
        ch.set_y_axis({"name": "Появлений" if metric == "count" else "Минут", "major_gridlines": {"visible": True, "line": {"color": "#E0E4EA"}}})
        ln.set_y2_axis({"num_format": "0%", "max": 1, "min": 0})
        ch.set_x_axis({"num_font": {"rotation": -30}})
        ch.set_legend({"position": "bottom"})
        ch.set_size({"width": 860, "height": 340})
        ws.insert_chart(row, 0, ch)
        row += 18
    else:
        ws.write(row, 0, "Нет аварий за выбранный период", f["muted"])
        row += 2

    def head(r: int, title: str, headers: list[str], num_from: int) -> int:
        ws.write(r, 0, title, f["title"])
        for c, h in enumerate(headers):
            ws.write(r + 1, c, h, f["head_r"] if c >= num_from else f["head"])
        ws.set_row(r + 1, 30)
        return r + 2

    def alarm_table(r: int, key: str, share_title: str, share_key: str) -> int:
        r = head(r, TABLE_TITLES[key], FREQ_HEADERS + [share_title], 4)
        for a in data[key]:
            ws.write(r, 0, a["plc"]); ws.write(r, 1, a["station"]); ws.write(r, 2, a["code"]); ws.write(r, 3, a["text"])
            ws.write_number(r, 4, a["count"], f["int"])
            ws.write_number(r, 5, a["total_sec"] / 60, f["n1"])
            ws.write_number(r, 6, a["avg_sec"] / 60, f["n1"])
            ws.write_number(r, 7, a["max_sec"] / 60, f["n1"])
            ws.write_number(r, 8, a[share_key], f["pct"])
            r += 1
        return r + 1

    ws.set_column(8, 8, 16)
    row = alarm_table(row, "frequent", SHARE_COUNT, "share_count")
    row = alarm_table(row, "longest", SHARE_TIME, "share_time")

    row = head(row, TABLE_TITLES["cases"], CASE_HEADERS, 5)
    for c in data["cases"]:
        ws.write(row, 0, c["plc"]); ws.write(row, 1, c["station"]); ws.write(row, 2, c["code"]); ws.write(row, 3, c["text"])
        ws.write_datetime(row, 4, to_plant(datetime.fromisoformat(c["started_at"].replace("Z", "+00:00"))), f["dt"])
        ws.write_number(row, 5, c["duration_sec"] / 60, f["n1"])
        row += 1
    row += 1

    row = head(row, TABLE_TITLES["stations"], STATION_HEADERS, 2)
    first = row
    for s in data["stations"]:
        ws.write(row, 0, s["plc"]); ws.write(row, 1, s["station"])
        ws.write_number(row, 2, s["count"], f["int"])
        ws.write_number(row, 3, s["total_sec"] / 60, f["n1"])
        ws.write_number(row, 4, s["avg_sec"] / 60, f["n1"])
        ws.write_number(row, 5, s["share_time"], f["pct"])
        row += 1
    if data["stations"]:
        a, b = first + 1, row
        ws.write(row, 0, "Итого", f["totl"]); ws.write(row, 1, "", f["totl"])
        ws.write_formula(row, 2, f"=SUM(C{a}:C{b})", f["intb"], sum(s["count"] for s in data["stations"]))
        tot_min = sum(s["total_sec"] for s in data["stations"]) / 60
        ws.write_formula(row, 3, f"=SUM(D{a}:D{b})", f["n1b"], tot_min)
        cnt = sum(s["count"] for s in data["stations"])
        ws.write_formula(row, 4, f"=IF(C{row + 1}=0,0,D{row + 1}/C{row + 1})", f["n1b"], tot_min / cnt if cnt else 0)
        ws.write_formula(row, 5, f"=SUM(F{a}:F{b})", f["pctb"], sum(s["share_time"] for s in data["stations"]))
        row += 1
    row += 1

    if d2 > d1:
        row = head(row, TABLE_TITLES["days"], DAY_HEADERS, 1)
        first = row
        for d in data["days"]:
            ws.write_datetime(row, 0, datetime.combine(date.fromisoformat(d["date"]), time.min), f["d"])
            ws.write_number(row, 1, d["count"], f["int"])
            ws.write_number(row, 2, d["total_sec"] / 60, f["n1"])
            ws.write_number(row, 3, d["avg_sec"] / 60, f["n1"])
            row += 1
        if data["days"]:
            a, b = first + 1, row
            cnt = sum(d["count"] for d in data["days"])
            tot_min = sum(d["total_sec"] for d in data["days"]) / 60
            ws.write(row, 0, "Итого", f["totl"])
            ws.write_formula(row, 1, f"=SUM(B{a}:B{b})", f["intb"], cnt)
            ws.write_formula(row, 2, f"=SUM(C{a}:C{b})", f["n1b"], tot_min)
            ws.write_formula(row, 3, f"=IF(B{row + 1}=0,0,C{row + 1}/B{row + 1})", f["n1b"], tot_min / cnt if cnt else 0)
    wb.close()
    return buf.getvalue()


@router.get("/analytics.xlsx", summary="Аналитика аварий в Excel за выбранный период и фильтр линии")
async def get_analytics_xlsx(
    request: Request,
    params: dict[str, Any] = Depends(_analytics_params),
    metric: str = Query("count", pattern="^(count|time)$"),
    src: str = Query("alarms"),
):
    from history import download_name, safe_name

    data = await fetch_analytics(request.app.state.pool, **params)
    xls = await run_in_threadpool(build_analytics_workbook, data, metric)
    d1, d2 = params["d1"], params["d2"]
    days = f"{d1.isoformat()}_{d2.isoformat()}" if d1 != d2 else d1.isoformat()
    fname = download_name(src, f"{safe_name(data['scope']['label'])}_{days}.xlsx")
    return Response(content=xls, media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
