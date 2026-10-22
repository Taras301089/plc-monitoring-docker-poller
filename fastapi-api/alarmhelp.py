"""«Аварии: что делать» (S-12, часть 2): для каждой аварии из журнала простое описание, частые причины и шаги исправления.

Номер аварии (plc_id + bit_index) тот же, что в alarm_catalog и alarm_events. Устройство как у signalnames.py:
- GET /api/alarm-help читает любой вошедший; создавать, править и делать заготовки могут все роли, кроме «Просмотр»;
- удаляет только автор строки и администратор; история «кто, когда, было, стало» в alarm_help_log, удалённое остаётся в журнале;
- alarm_help_for(pool, plc_id, bit_index) -> {simple, causes, fix} или None: ею пользуется why.py;
- GET /api/alarm-help.xlsx повторяет экран.
"""
from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth import require_editor, require_user
from signalnames import STATUSES, XLSX, _clean, who

router = APIRouter(prefix="/api")

MAX_SRC = 500
MAX_SIMPLE = 400
MAX_LONG = 1500       # причины и шаги: несколько строк текста

COLUMNS: list[tuple[str, str]] = [
    ("plc", "ПЛК"), ("bit_index", "№ аварии"), ("alarm_text_src", "Исходный текст аварии"), ("simple_description", "Что случилось"),
    ("typical_causes", "Частые причины"), ("fix_steps", "Что сделать"), ("status", "Статус"), ("author", "Автор"),
]
XLS_WIDTHS = [14, 10, 44, 40, 40, 50, 12, 26]
SHEET = "Аварии, что делать"
FIELDS = ("alarm_text_src", "simple_description", "typical_causes", "fix_steps", "status")


async def ensure_alarm_help_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_help (
            id                 SERIAL PRIMARY KEY,
            plc_id             INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
            bit_index          INTEGER     NOT NULL,
            alarm_text_src     TEXT        NOT NULL DEFAULT '',
            simple_description TEXT        NOT NULL DEFAULT '',
            typical_causes     TEXT        NOT NULL DEFAULT '',
            fix_steps          TEXT        NOT NULL DEFAULT '',
            status             TEXT        NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'checked')),
            created_by         INTEGER,
            created_by_name    TEXT        NOT NULL DEFAULT '',
            updated_by_name    TEXT        NOT NULL DEFAULT '',
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (plc_id, bit_index)
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_help_log (
            id        SERIAL PRIMARY KEY,
            help_id   INTEGER     NOT NULL,
            plc_id    INTEGER     NOT NULL,
            bit_index INTEGER     NOT NULL,
            action    TEXT        NOT NULL CHECK (action IN ('create', 'update', 'delete')),
            was       JSONB,
            now       JSONB,
            by_name   TEXT        NOT NULL DEFAULT '',
            at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_alarm_help_log_help ON alarm_help_log (help_id, at)")


def _fields(r: Any) -> dict[str, str]:
    return {k: r[k] for k in FIELDS}


def _multi(s: str) -> str:
    """Строки и абзацы сохраняются (шаги по пунктам), лишние пробелы внутри строки убираются."""
    return "\n".join(" ".join(line.split()) for line in (s or "").strip().splitlines())


class HelpBody(BaseModel):
    alarm_text_src: str = Field(default="", max_length=MAX_SRC)
    simple_description: str = Field(default="", max_length=MAX_SIMPLE)
    typical_causes: str = Field(default="", max_length=MAX_LONG)
    fix_steps: str = Field(default="", max_length=MAX_LONG)
    status: str = "draft"


class HelpCreate(HelpBody):
    plc_id: int
    bit_index: int = Field(ge=0)


def _check(body: HelpBody) -> dict[str, str]:
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail="Статус должен быть «черновик» или «проверено»")
    f = {"alarm_text_src": _clean(body.alarm_text_src), "simple_description": _multi(body.simple_description),
         "typical_causes": _multi(body.typical_causes), "fix_steps": _multi(body.fix_steps), "status": body.status}
    if f["status"] == "checked" and not (f["simple_description"] and f["fix_steps"]):
        raise HTTPException(status_code=422, detail="Нельзя отметить «проверено», пока не заполнены «Что случилось» и «Что сделать»")
    return f


def _item(r: Any, user: dict[str, Any]) -> dict[str, Any]:
    can_del = user["role"] == "admin" or (r["created_by"] is not None and r["created_by"] == user["id"])
    return {"id": r["id"], "plc_id": r["plc_id"], "plc": r["plc_name"], "bit_index": r["bit_index"], **_fields(r),
            "status_name": STATUSES[r["status"]], "author": r["created_by_name"], "updated_by": r["updated_by_name"],
            "updated_at": r["updated_at"].isoformat(), "can_delete": can_del}


_SELECT = "SELECT h.*, p.name AS plc_name FROM alarm_help h JOIN plcs p ON p.id = h.plc_id"


async def list_items(pool: asyncpg.Pool, plc_id: int | None, status: str | None, q: str | None, user: dict[str, Any]) -> list[dict[str, Any]]:
    where, args = ["TRUE"], []
    if plc_id is not None:
        args.append(plc_id)
        where.append(f"h.plc_id = ${len(args)}")
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail="Неизвестный статус")
        args.append(status)
        where.append(f"h.status = ${len(args)}")
    if q and q.strip():
        args.append("%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where.append(f"lower(h.alarm_text_src || ' ' || h.simple_description || ' ' || h.typical_causes || ' ' || h.fix_steps || ' ' || h.bit_index) LIKE ${len(args)}")
    rows = await pool.fetch(f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY p.name, h.bit_index", *args)
    return [_item(r, user) for r in rows]


async def alarm_help_for(pool: asyncpg.Pool, plc_id: int, bit_index: int, only_checked: bool = False) -> dict[str, str] | None:
    """{simple, causes, fix} для аварии или None, если справки нет или в ней ничего не заполнено.
    only_checked=True отдаёт только строки со статусом «проверено»."""
    r = await pool.fetchrow(
        "SELECT simple_description, typical_causes, fix_steps, status FROM alarm_help WHERE plc_id = $1 AND bit_index = $2", plc_id, bit_index)
    if not r or not (r["simple_description"] or r["typical_causes"] or r["fix_steps"]) or (only_checked and r["status"] != "checked"):
        return None
    return {"simple": r["simple_description"], "causes": r["typical_causes"], "fix": r["fix_steps"]}


async def _log(con: asyncpg.Connection, hid: int, plc_id: int, bit: int, action: str, was: Any, now: Any, by: str) -> None:
    await con.execute(
        "INSERT INTO alarm_help_log (help_id, plc_id, bit_index, action, was, now, by_name) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)",
        hid, plc_id, bit, action, json.dumps(was, ensure_ascii=False) if was is not None else None,
        json.dumps(now, ensure_ascii=False) if now is not None else None, by,
    )


async def _insert(con: asyncpg.Connection, plc_id: int, bit: int, f: dict[str, str], user: dict[str, Any]) -> int | None:
    name = who(user)
    hid = await con.fetchval(
        "INSERT INTO alarm_help (plc_id, bit_index, alarm_text_src, simple_description, typical_causes, fix_steps, status, created_by, created_by_name, updated_by_name) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9) ON CONFLICT (plc_id, bit_index) DO NOTHING RETURNING id",
        plc_id, bit, f["alarm_text_src"], f["simple_description"], f["typical_causes"], f["fix_steps"], f["status"], user["id"], name,
    )
    if hid is not None:
        await _log(con, hid, plc_id, bit, "create", None, f, name)
    return hid


async def _catalog_text(con: Any, plc_id: int, bit: int) -> str:
    r = await con.fetchrow("SELECT code, text FROM alarm_catalog WHERE plc_id = $1 AND bit_index = $2", plc_id, bit)
    return _clean(" ".join(x for x in (r["code"], r["text"]) if x))[:MAX_SRC] if r else ""


async def _need_plc(pool: asyncpg.Pool, plc_id: int) -> None:
    if await pool.fetchval("SELECT 1 FROM plcs WHERE id = $1", plc_id) is None:
        raise HTTPException(status_code=404, detail="ПЛК не найден")


@router.get("/alarm-help", summary="Аварии: что делать — справка по авариям (читает любой вошедший)")
async def get_alarm_help(request: Request, plc_id: int | None = None, status: str | None = None, q: str | None = None,
                         user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    items = await list_items(pool, plc_id, status, q, user)
    plcs = await pool.fetch("SELECT id, name FROM plcs ORDER BY name")
    return {"items": items, "columns": [{"k": k, "t": t} for k, t in COLUMNS], "plcs": [{"id": p["id"], "name": p["name"]} for p in plcs],
            "limits": {"alarm_text_src": MAX_SRC, "simple_description": MAX_SIMPLE, "typical_causes": MAX_LONG, "fix_steps": MAX_LONG}}


@router.post("/alarm-help", summary="Добавить справку по аварии (все роли, кроме «Просмотр»)", status_code=201)
async def create_alarm_help(body: HelpCreate, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    await _need_plc(pool, body.plc_id)
    f = _check(body)
    async with pool.acquire() as con, con.transaction():
        if not f["alarm_text_src"]:
            f["alarm_text_src"] = await _catalog_text(con, body.plc_id, body.bit_index)
        hid = await _insert(con, body.plc_id, body.bit_index, f, user)
    if hid is None:
        raise HTTPException(status_code=409, detail="Для этой аварии справка уже есть в таблице")
    return {"id": hid}


@router.put("/alarm-help/{hid}", summary="Изменить описание, причины, шаги и статус аварии (все роли, кроме «Просмотр»)")
async def update_alarm_help(hid: int, body: HelpBody, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    f = _check(body)
    name = who(user)
    async with pool.acquire() as con, con.transaction():
        r = await con.fetchrow("SELECT * FROM alarm_help WHERE id = $1 FOR UPDATE", hid)
        if r is None:
            raise HTTPException(status_code=404, detail="Строка не найдена")
        was = _fields(r)
        if was != f:
            await con.execute(
                "UPDATE alarm_help SET alarm_text_src = $2, simple_description = $3, typical_causes = $4, fix_steps = $5, status = $6, "
                "updated_by_name = $7, updated_at = now() WHERE id = $1",
                hid, f["alarm_text_src"], f["simple_description"], f["typical_causes"], f["fix_steps"], f["status"], name,
            )
            await _log(con, hid, r["plc_id"], r["bit_index"], "update", was, f, name)
    return {"id": hid, "changed": was != f}


@router.delete("/alarm-help/{hid}", summary="Удалить строку (только автор строки и администратор; история остаётся в журнале)")
async def delete_alarm_help(hid: int, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        r = await con.fetchrow("SELECT * FROM alarm_help WHERE id = $1 FOR UPDATE", hid)
        if r is None:
            raise HTTPException(status_code=404, detail="Строка не найдена")
        if user["role"] != "admin" and r["created_by"] != user["id"]:
            raise HTTPException(status_code=403, detail="Удалить строку может только её автор или администратор")
        await _log(con, hid, r["plc_id"], r["bit_index"], "delete", _fields(r), None, who(user))
        await con.execute("DELETE FROM alarm_help WHERE id = $1", hid)
    return {"id": hid}


@router.get("/alarm-help/{hid}/log", summary="История изменений строки: кто, когда, было, стало (читает любой вошедший)")
async def alarm_help_log(hid: int, request: Request, _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await request.app.state.pool.fetch("SELECT action, was, now, by_name, at FROM alarm_help_log WHERE help_id = $1 ORDER BY id", hid)
    return {"items": [{"action": r["action"], "was": json.loads(r["was"]) if r["was"] else None, "now": json.loads(r["now"]) if r["now"] else None,
                       "by": r["by_name"], "at": r["at"].isoformat()} for r in rows]}


class DraftsBody(BaseModel):
    plc_id: int
    limit: int = Field(default=20, ge=1, le=100)
    days: int = Field(default=7, ge=1, le=90)


@router.post("/alarm-help/drafts", summary="Создать заготовки для самых частых аварий ПЛК за последние дни (текст из каталога аварий)")
async def make_drafts(body: DraftsBody, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    await _need_plc(pool, body.plc_id)
    rows = await pool.fetch(
        """
        SELECT c.bit_index, c.code, c.text, count(*) AS n
        FROM alarm_events e JOIN alarm_catalog c ON c.plc_id = e.plc_id AND c.bit_index = e.bit_index
        WHERE e.plc_id = $1 AND e.started_at >= now() - make_interval(days => $2) AND NOT c.is_placeholder
        GROUP BY c.bit_index, c.code, c.text ORDER BY n DESC, c.bit_index LIMIT $3
        """,
        body.plc_id, body.days, body.limit,
    )
    added = 0
    async with pool.acquire() as con, con.transaction():
        for r in rows:
            src = _clean(" ".join(x for x in (r["code"], r["text"]) if x))[:MAX_SRC]
            f = {"alarm_text_src": src, "simple_description": "", "typical_causes": "", "fix_steps": "", "status": "draft"}
            if await _insert(con, body.plc_id, r["bit_index"], f, user) is not None:
                added += 1
    return {"added": added, "skipped": len(rows) - added, "found": len(rows)}


def _cells(i: dict[str, Any]) -> list[Any]:
    return [i["plc"], str(i["bit_index"]), i["alarm_text_src"], i["simple_description"], i["typical_causes"], i["fix_steps"], i["status_name"], i["author"]]


def build_workbook(items: list[dict[str, Any]], plc_name: str, status_name: str, query: str) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(SHEET[:31])
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    ws.write(0, 0, SHEET, wb.add_format({"bold": True, "font_size": 14}))
    flt = [f"ПЛК: {plc_name or 'все'}", f"Статус: {status_name or 'все'}"] + ([f"Поиск: {query}"] if query else [])
    ws.write(1, 0, ". ".join(flt), wb.add_format({"bold": True}))
    top = 3
    for c, (_k, h) in enumerate(COLUMNS):
        ws.write(top, c, h, head)
        ws.set_column(c, c, XLS_WIDTHS[c])
    ws.set_row(top, 30)
    for n, it in enumerate(items, start=1):
        for c, v in enumerate(_cells(it)):
            ws.write_string(top + n, c, v, wrap)
    ws.autofilter(top, 0, top + max(len(items), 1), len(COLUMNS) - 1)
    ws.freeze_panes(top + 1, 0)
    wb.close()
    return buf.getvalue()


@router.get("/alarm-help.xlsx", summary="Аварии: что делать — в Excel (по тем же фильтрам, что на экране)")
async def alarm_help_xlsx(request: Request, plc_id: int | None = None, status: str | None = None, q: str | None = None,
                          user: dict[str, Any] = Depends(require_user)) -> Response:
    from alarms import plant_today
    from history import download_name, safe_name

    pool: asyncpg.Pool = request.app.state.pool
    items = await list_items(pool, plc_id, status, q, user)
    plc_name = ""
    if plc_id is not None:
        await _need_plc(pool, plc_id)
        plc_name = await pool.fetchval("SELECT name FROM plcs WHERE id = $1", plc_id)
    xls = await run_in_threadpool(build_workbook, items, plc_name, STATUSES.get(status or "", ""), (q or "").strip())
    fname = download_name("alarm_help", f"{safe_name(plc_name or 'все ПЛК')}_{plant_today().isoformat()}.xlsx")
    return Response(content=xls, media_type=XLSX, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
