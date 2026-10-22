"""Простые названия сигналов ПЛК (S-12): «сигнал ПЛК → простое название и где смотреть» для ответов киповцам.

Бот в Telegram (S-8…S-11) пишет слесарю не «ITL_GWM_DB."From GWM".BodyCheck1», а «датчик положения кузова на выдаче; где смотреть:
станция MB130». Таблица signal_names хранит эти названия; заполняет человек (Тарас), заготовки создаются из логики ПЛК и журнала аварий.
- GET /api/signal-names (список с фильтрами) читает любой вошедший; создавать, править и создавать заготовки могут все роли, кроме «Просмотр»;
- удаляет только автор строки и администратор; каждое изменение пишется в signal_names_log (кто, когда, было, стало), удалённое остаётся в журнале;
- friendly(pool, plc_id, tag) возвращает (простое название, где смотреть) или None: ею пользуется why.py (S-8);
- GET /api/signal-names.xlsx повторяет экран (те же колонки, фильтры, порядок).
"""
from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

import tia
import tia_logic
from auth import ROLES, require_editor, require_user

router = APIRouter(prefix="/api")

STATUSES = {"draft": "черновик", "checked": "проверено"}
MAX_NAME = 120        # простое название должно быть коротким: оно уходит в сообщение Telegram
MAX_PLACE = 160
MAX_SOURCE = 500
MAX_TAG = 300
MAX_ROWS = 20000
LEAF_DEPTH = 6        # на сколько шагов вглубь идти по промежуточным сигналам (выход, записанный другой катушкой)
LEAF_CAP = 400
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Колонки экрана и Excel: ключ, заголовок. Экран (signalnames.js) и тест сверяют свои заголовки с этим списком.
COLUMNS: list[tuple[str, str]] = [
    ("plc", "ПЛК"), ("tag", "Сигнал (технический)"), ("simple_name", "Простое название"), ("place", "Где смотреть"),
    ("status", "Статус"), ("source_text", "Исходный комментарий"), ("author", "Автор"),
]
XLS_WIDTHS = [14, 44, 36, 30, 12, 44, 26]
SHEET = "Простые названия"


def tag_key(tag: str) -> str:
    """Ключ поиска: как в tia_logic.norm_name (без кавычек и регистра), лишние пробелы по краям убраны."""
    return tia_logic.norm_name((tag or "").strip())


async def ensure_signal_names_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_names (
            id              SERIAL PRIMARY KEY,
            plc_id          INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
            tag             TEXT        NOT NULL,
            tag_key         TEXT        NOT NULL,
            simple_name     TEXT        NOT NULL DEFAULT '',
            place           TEXT        NOT NULL DEFAULT '',
            status          TEXT        NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'checked')),
            source_text     TEXT        NOT NULL DEFAULT '',
            created_by      INTEGER,
            created_by_name TEXT        NOT NULL DEFAULT '',
            updated_by_name TEXT        NOT NULL DEFAULT '',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (plc_id, tag_key)
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_names_log (
            id        SERIAL PRIMARY KEY,
            signal_id INTEGER     NOT NULL,
            plc_id    INTEGER     NOT NULL,
            tag       TEXT        NOT NULL,
            action    TEXT        NOT NULL CHECK (action IN ('create', 'update', 'delete')),
            was       JSONB,
            now       JSONB,
            by_name   TEXT        NOT NULL DEFAULT '',
            at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_signal_names_log_signal ON signal_names_log (signal_id, at)")


def who(user: dict[str, Any]) -> str:
    """Автор как «Фамилия, должность» (единый порядок проекта)."""
    pos = ROLES.get(user.get("role") or "", "")
    last = user.get("last_name") or user.get("login") or ""
    return f"{last}, {pos}" if last and pos else last


def _fields(r: Any) -> dict[str, str]:
    return {"simple_name": r["simple_name"], "place": r["place"], "status": r["status"], "source_text": r["source_text"]}


def _clean(s: str) -> str:
    return " ".join((s or "").split())


class SignalBody(BaseModel):
    simple_name: str = Field(default="", max_length=MAX_NAME)
    place: str = Field(default="", max_length=MAX_PLACE)
    status: str = "draft"
    source_text: str = Field(default="", max_length=MAX_SOURCE)


class SignalCreate(SignalBody):
    plc_id: int
    tag: str = Field(min_length=1, max_length=MAX_TAG)


def _check(body: SignalBody) -> dict[str, str]:
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail="Статус должен быть «черновик» или «проверено»")
    f = {"simple_name": _clean(body.simple_name), "place": _clean(body.place), "status": body.status, "source_text": _clean(body.source_text)}
    if f["status"] == "checked" and not f["simple_name"]:
        raise HTTPException(status_code=422, detail="Нельзя отметить «проверено», пока не заполнено простое название")
    return f


def _item(r: Any, user: dict[str, Any] | None) -> dict[str, Any]:
    can_del = bool(user and (user["role"] == "admin" or (r["created_by"] is not None and r["created_by"] == user["id"])))
    return {
        "id": r["id"], "plc_id": r["plc_id"], "plc": r["plc_name"], "tag": r["tag"], "simple_name": r["simple_name"], "place": r["place"],
        "status": r["status"], "status_name": STATUSES[r["status"]], "source_text": r["source_text"], "author": r["created_by_name"],
        "updated_by": r["updated_by_name"], "updated_at": r["updated_at"].isoformat(), "can_delete": can_del,
    }


_SELECT = "SELECT s.*, p.name AS plc_name FROM signal_names s JOIN plcs p ON p.id = s.plc_id"


async def list_items(pool: asyncpg.Pool, plc_id: int | None, status: str | None, q: str | None, user: dict[str, Any] | None,
                     limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    where, args = ["TRUE"], []
    if plc_id is not None:
        args.append(plc_id)
        where.append(f"s.plc_id = ${len(args)}")
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail="Неизвестный статус")
        args.append(status)
        where.append(f"s.status = ${len(args)}")
    if q and q.strip():
        args.append("%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where.append(f"lower(s.tag || ' ' || s.simple_name || ' ' || s.place || ' ' || s.source_text) LIKE ${len(args)}")
    args.append(limit)
    rows = await pool.fetch(f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY p.name, s.tag_key LIMIT ${len(args)}", *args)
    return [_item(r, user) for r in rows]


async def friendly(pool: asyncpg.Pool, plc_id: int, tag: str, only_checked: bool = False) -> tuple[str, str] | None:
    """(простое название, где смотреть) для сигнала или None, если названия нет или оно ещё не заполнено.
    only_checked=True отдаёт только строки со статусом «проверено». Имя сигнала сравнивается как в tia_logic.norm_name."""
    r = await pool.fetchrow("SELECT simple_name, place, status FROM signal_names WHERE plc_id = $1 AND tag_key = $2", plc_id, tag_key(tag))
    if not r or not r["simple_name"] or (only_checked and r["status"] != "checked"):
        return None
    return r["simple_name"], r["place"]


async def _log(con: asyncpg.Connection, sid: int, plc_id: int, tag: str, action: str, was: Any, now: Any, by: str) -> None:
    await con.execute(
        "INSERT INTO signal_names_log (signal_id, plc_id, tag, action, was, now, by_name) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)",
        sid, plc_id, tag, action, json.dumps(was, ensure_ascii=False) if was is not None else None,
        json.dumps(now, ensure_ascii=False) if now is not None else None, by,
    )


async def _insert(con: asyncpg.Connection, plc_id: int, tag: str, f: dict[str, str], user: dict[str, Any]) -> int | None:
    """Новая строка; None, если сигнал уже есть. Пишет журнал."""
    name = who(user)
    sid = await con.fetchval(
        "INSERT INTO signal_names (plc_id, tag, tag_key, simple_name, place, status, source_text, created_by, created_by_name, updated_by_name) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9) ON CONFLICT (plc_id, tag_key) DO NOTHING RETURNING id",
        plc_id, tag, tag_key(tag), f["simple_name"], f["place"], f["status"], f["source_text"], user["id"], name,
    )
    if sid is not None:
        await _log(con, sid, plc_id, tag, "create", None, f, name)
    return sid


async def _plc_name(pool: asyncpg.Pool, plc_id: int) -> str:
    n = await pool.fetchval("SELECT name FROM plcs WHERE id = $1", plc_id)
    if n is None:
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    return n


@router.get("/signal-names", summary="Простые названия сигналов ПЛК (читает любой вошедший)")
async def get_signal_names(request: Request, plc_id: int | None = None, status: str | None = None, q: str | None = None,
                           user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    items = await list_items(pool, plc_id, status, q, user)
    plcs = await pool.fetch("SELECT id, name FROM plcs ORDER BY name")
    return {"items": items, "columns": [{"k": k, "t": t} for k, t in COLUMNS], "plcs": [{"id": p["id"], "name": p["name"]} for p in plcs],
            "limits": {"simple_name": MAX_NAME, "place": MAX_PLACE, "source_text": MAX_SOURCE}}


@router.post("/signal-names", summary="Добавить простое название сигнала (все роли, кроме «Просмотр»)", status_code=201)
async def create_signal_name(body: SignalCreate, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    await _plc_name(pool, body.plc_id)
    f = _check(body)
    tag = _clean(body.tag)
    if not tag_key(tag):
        raise HTTPException(status_code=422, detail="Укажите технический сигнал")
    async with pool.acquire() as con, con.transaction():
        sid = await _insert(con, body.plc_id, tag, f, user)
    if sid is None:
        raise HTTPException(status_code=409, detail="Такой сигнал для этого ПЛК уже есть в таблице")
    return {"id": sid}


@router.put("/signal-names/{sid}", summary="Изменить простое название, место, статус, комментарий (все роли, кроме «Просмотр»)")
async def update_signal_name(sid: int, body: SignalBody, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    f = _check(body)
    name = who(user)
    async with pool.acquire() as con, con.transaction():
        r = await con.fetchrow("SELECT * FROM signal_names WHERE id = $1 FOR UPDATE", sid)
        if r is None:
            raise HTTPException(status_code=404, detail="Строка не найдена")
        was = _fields(r)
        if was != f:
            await con.execute(
                "UPDATE signal_names SET simple_name = $2, place = $3, status = $4, source_text = $5, updated_by_name = $6, updated_at = now() WHERE id = $1",
                sid, f["simple_name"], f["place"], f["status"], f["source_text"], name,
            )
            await _log(con, sid, r["plc_id"], r["tag"], "update", was, f, name)
    return {"id": sid, "changed": was != f}


@router.delete("/signal-names/{sid}", summary="Удалить строку (только автор строки и администратор; история остаётся в журнале)")
async def delete_signal_name(sid: int, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        r = await con.fetchrow("SELECT * FROM signal_names WHERE id = $1 FOR UPDATE", sid)
        if r is None:
            raise HTTPException(status_code=404, detail="Строка не найдена")
        if user["role"] != "admin" and r["created_by"] != user["id"]:
            raise HTTPException(status_code=403, detail="Удалить строку может только её автор или администратор")
        await _log(con, sid, r["plc_id"], r["tag"], "delete", _fields(r), None, who(user))
        await con.execute("DELETE FROM signal_names WHERE id = $1", sid)
    return {"id": sid}


@router.get("/signal-names/{sid}/log", summary="История изменений строки: кто, когда, было, стало (читает любой вошедший)")
async def signal_name_log(sid: int, request: Request, _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await request.app.state.pool.fetch("SELECT action, was, now, by_name, at FROM signal_names_log WHERE signal_id = $1 ORDER BY id", sid)
    out = [{"action": r["action"], "was": json.loads(r["was"]) if r["was"] else None, "now": json.loads(r["now"]) if r["now"] else None,
            "by": r["by_name"], "at": r["at"].isoformat()} for r in rows]
    return {"items": out}


# ------------------------------------------------------------------ заготовки
def collect_leaves(logic: tia_logic.TiaLogic, output: str, depth: int = LEAF_DEPTH, cap: int = LEAF_CAP) -> dict[str, str]:
    """Листья цепочки выхода: сигналы, которые сама логика нигде не записывает (датчики, кнопки, сигналы другой системы).
    Промежуточные сигналы (записанные другой катушкой) раскрываются дальше до depth шагов; сам выход и повторы пропускаются.
    Возвращает {ключ: имя как в логике}."""
    seen = {tia_logic.norm_name(output)}
    res: dict[str, str] = {}

    def walk(name: str, d: int) -> None:
        for e in logic.chains_for(name, partial=(d == 1)):
            for nm in sorted(tia_logic.leaf_names([e.get("c"), e.get("v"), e.get("call")])):
                k = tia_logic.norm_name(nm)
                if k in seen or len(res) >= cap:
                    continue
                seen.add(k)
                if logic.chains_for(nm):
                    if d < depth:
                        walk(nm, d + 1)
                else:
                    res[k] = nm

    walk(output, 1)
    return res


async def _project_for_plc(pool: asyncpg.Pool, plc_id: int, project_id: str | None) -> dict[str, str]:
    name = await _plc_name(pool, plc_id)
    projects = tia.load_projects()
    if project_id:
        p = next((x for x in projects if x["id"] == project_id), None)
    else:
        p = next((x for x in projects if (x.get("plc") or "").casefold() == name.casefold()), None)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Для ПЛК «{name}» нет проекта TIA в tia-projects.json (поле plc): заготовки из логики недоступны")
    return p


def _load_logic(project_id: str) -> tia_logic.TiaLogic:
    path, _st = tia._logic_paths(project_id)
    if not path.is_file():
        raise HTTPException(status_code=409, detail="Файл логики ещё не создан: выгрузите проект из TIA и создайте «Файл для ИИ»")
    return tia_logic.TiaLogic.from_json(json.loads(path.read_text(encoding="utf-8")))


class DraftsBody(BaseModel):
    plc_id: int
    output: str = Field(min_length=1, max_length=MAX_TAG)
    project_id: str | None = None


class AlarmDraftsBody(BaseModel):
    plc_id: int
    limit: int = Field(default=20, ge=1, le=100)
    days: int = Field(default=7, ge=1, le=90)


async def _add_drafts(pool: asyncpg.Pool, plc_id: int, rows: list[tuple[str, str]], user: dict[str, Any]) -> dict[str, int]:
    """rows: (сигнал, исходный комментарий). Существующие строки не трогаются."""
    added = 0
    async with pool.acquire() as con, con.transaction():
        for tag, src in rows:
            if await _insert(con, plc_id, tag, {"simple_name": "", "place": "", "status": "draft", "source_text": _clean(src)[:MAX_SOURCE]}, user) is not None:
                added += 1
    return {"added": added, "skipped": len(rows) - added}


@router.post("/signal-names/drafts", summary="Создать заготовки (черновики) из логики ПЛК: листья цепочки выбранного выхода")
async def make_drafts(body: DraftsBody, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    project = await _project_for_plc(pool, body.plc_id, body.project_id)
    logic = await run_in_threadpool(_load_logic, project["id"])
    output = _clean(body.output)
    if not logic.chains_for(output, partial=True):
        raise HTTPException(status_code=404, detail="Выход не найден в логике ПЛК: проверьте имя, например OPMode08_DB.EL300.Goto3rd")
    leaves = await run_in_threadpool(collect_leaves, logic, output)
    if not leaves:
        raise HTTPException(status_code=404, detail="У цепочки этого выхода нет входных сигналов")
    res = await _add_drafts(pool, body.plc_id, [(nm, "") for nm in leaves.values()], user)
    return {**res, "found": len(leaves), "project": project["id"]}


@router.post("/signal-names/drafts-alarms", summary="Создать заготовки из самых частых сообщений журнала аварий ПЛК за последние дни")
async def make_alarm_drafts(body: AlarmDraftsBody, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    await _plc_name(pool, body.plc_id)
    rows = await pool.fetch(
        """
        SELECT c.bit_index, c.alarm_name, c.trigger_tag, c.code, c.text, c.station, count(*) AS n
        FROM alarm_events e JOIN alarm_catalog c ON c.plc_id = e.plc_id AND c.bit_index = e.bit_index
        WHERE e.plc_id = $1 AND e.started_at >= now() - make_interval(days => $2) AND NOT c.is_placeholder
        GROUP BY c.bit_index, c.alarm_name, c.trigger_tag, c.code, c.text, c.station
        ORDER BY n DESC, c.bit_index LIMIT $3
        """,
        body.plc_id, body.days, body.limit,
    )
    items = []
    for r in rows:
        tag = (r["trigger_tag"] or r["alarm_name"] or "").strip() or f"alarm_bit_{r['bit_index']}"
        src = " ".join(x for x in (r["code"], r["text"]) if x)
        items.append((tag, src))
    res = await _add_drafts(pool, body.plc_id, items, user)
    return {**res, "found": len(items)}


# ------------------------------------------------------------------ Excel
def _cells(item: dict[str, Any]) -> list[str]:
    return [item["plc"], item["tag"], item["simple_name"], item["place"], item["status_name"], item["source_text"], item["author"]]


def build_workbook(items: list[dict[str, Any]], plc_name: str, status_name: str, query: str) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(SHEET)
    title = wb.add_format({"bold": True, "font_size": 14})
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    ws.write(0, 0, SHEET, title)
    flt = [f"ПЛК: {plc_name or 'все'}", f"Статус: {status_name or 'все'}"] + ([f"Поиск: {query}"] if query else [])
    ws.write(1, 0, ". ".join(flt), wb.add_format({"bold": True}))
    top = 3
    for c, (_k, h) in enumerate(COLUMNS):
        ws.write(top, c, h, head)
        ws.set_column(c, c, XLS_WIDTHS[c])
    ws.set_row(top, 30)
    for i, it in enumerate(items, start=1):
        for c, v in enumerate(_cells(it)):
            ws.write_string(top + i, c, v, wrap)
    ws.autofilter(top, 0, top + max(len(items), 1), len(COLUMNS) - 1)
    ws.freeze_panes(top + 1, 0)
    wb.close()
    return buf.getvalue()


@router.get("/signal-names.xlsx", summary="Простые названия сигналов в Excel (по тем же фильтрам, что на экране)")
async def signal_names_xlsx(request: Request, plc_id: int | None = None, status: str | None = None, q: str | None = None,
                            user: dict[str, Any] = Depends(require_user)) -> Response:
    from alarms import plant_today
    from history import download_name, safe_name

    pool: asyncpg.Pool = request.app.state.pool
    items = await list_items(pool, plc_id, status, q, user)
    plc_name = await _plc_name(pool, plc_id) if plc_id is not None else ""
    xls = await run_in_threadpool(build_workbook, items, plc_name, STATUSES.get(status or "", ""), (q or "").strip())
    fname = download_name("signals", f"{safe_name(plc_name or 'все ПЛК')}_{plant_today().isoformat()}.xlsx")
    return Response(content=xls, media_type=XLSX, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
