"""Простои на экранах Andon: автоматические простои (их считает сборщик KPI в поллере) и их описание пользователями."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import dayflags
from auth import ROLES, require_editor
from dictionary import SCOPES

OTHER = "Другое"
MODERATORS = {"admin", "chief", "area_head"}

# Ответственный за отставание: отдел, к которому относится причина. Сокращения и статус считает только этот модуль
OWNERS = {"production": "ПР", "oto": "ОТО", "ito": "ИТО", "other": "ДР"}
NO_OWNER = "none"          # ответственный не назначен: пока человек не выбрал отдел, строка не относится ни к ПР, ни к ОТО, ни к ИТО, ни к ДР
OWNER_NAMES = {"production": "Производство", "oto": "ОТО", "ito": "ИТО", "other": "Другое (внешние причины)"}
# знак на экране и в Excel: «○» ждёт ответа, «●» принято полностью, «◐» принято частично (только отображение: суммы считает owner_status), «!» спорно
STATUS_MARKS = {"pending": "○", "confirmed": "●", "partial": "◐", "disputed": "!"}
STATUS_NAMES = {"pending": "ждёт подтверждения", "confirmed": "принято полностью", "partial": "принято частично", "disputed": "спорно"}

router = APIRouter(prefix="/api/kpi")


ANSWERING = ("oto", "ito")   # отделы, которые отвечают на ответственность (у ПР и ДР ответа нет, всегда «подтверждено»)
ANSWERS = {"agree": "Согласен", "partial": "Частично", "disagree": "Не согласен"}


def owner_status(owner: str | None, answer: str | None = None) -> str:
    """Статус ответственного по ответу отдела: у ПР и ДР всегда «подтверждено»; у ОТО и ИТО без ответа «ждёт подтверждения»,
    «Согласен» и «Частично» дают «подтверждено», «Не согласен» даёт «спорно». answer: ответ ТОГО ЖЕ отдела, что сейчас ответственный."""
    if owner not in ANSWERING:
        return "confirmed"
    return {"agree": "confirmed", "partial": "confirmed", "disagree": "disputed"}.get(answer or "", "pending")


def owner_mark(owner: str | None, answer: str | None = None) -> str:
    """Состояние для знака: как owner_status, но ответ «Частично» отдела ОТО/ИТО показывается отдельным знаком «◐». В итогах не участвует."""
    if owner in ANSWERING and answer == "partial":
        return "partial"
    return owner_status(owner, answer)


# ответ отдела к строке описания: только ответ того отдела, который сейчас ответственный (после смены ОТО → ИТО прежний ответ не действует)
RESP_JOIN = ("LEFT JOIN kpi_downtime_responses rp ON rp.item_id = i.id AND rp.department = i.owner "
             "LEFT JOIN app_users ru ON ru.id = rp.user_id")
RESP_COLS = ("rp.answer AS resp_answer, rp.minutes AS resp_minutes, rp.comment AS resp_comment, rp.user_id AS resp_user_id, "
             "rp.user_name AS resp_user_name, rp.updated_at AS resp_updated_at, ru.last_name AS resp_last, ru.role AS resp_role")


def response_view(r: Any) -> dict[str, Any] | None:
    """Ответ отдела для экрана: что ответили, сколько минут принял отдел (только «Частично»), комментарий, кто и когда."""
    if r["resp_answer"] is None:
        return None
    return {"answer": r["resp_answer"], "minutes": None if r["resp_minutes"] is None else float(r["resp_minutes"]),
            "comment": r["resp_comment"] or "", "user_id": r["resp_user_id"], "user_name": r["resp_user_name"] or "",
            "author": _author(r["resp_last"], r["resp_role"], r["resp_user_name"] or ""), "updated_at": r["resp_updated_at"]}


def owner_label(owner: str | None, status: str | None = None) -> str:
    """Сокращение со знаком статуса, одинаковое на Andon, во «Простоях» и в Excel: «ПР ●», «ОТО ○», «ИТО !», «ОТО ◐»."""
    if owner not in OWNERS:
        return "—"            # не назначен: ни сокращения отдела, ни знака статуса
    return f"{OWNERS[owner]} {STATUS_MARKS[status or owner_status(owner)]}"


async def ensure_downtime_schema(pool: asyncpg.Pool) -> None:
    # Простой интервала: создаётся сборщиком, когда по завершённому интервалу факт меньше плана.
    # source: 'plan' = расчёт по отставанию от плана; позже добавится 'plc' = время простоя из триггеров ПЛК.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtimes (
            id           SERIAL PRIMARY KEY,
            screen_id    INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
            prod_date    DATE    NOT NULL,
            idx          INTEGER NOT NULL,
            source       TEXT    NOT NULL DEFAULT 'plan',
            plan         INTEGER NOT NULL DEFAULT 0,
            fact         INTEGER NOT NULL DEFAULT 0,
            interval_min INTEGER NOT NULL DEFAULT 0,
            minutes      NUMERIC(6, 1) NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (screen_id, prod_date, idx, source)
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_downtimes_date ON kpi_downtimes (prod_date)")
    # Строки описания простоя: участок / станция, причина, минуты, пояснение; кто внёс и кто менял последним
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_items (
            id          SERIAL PRIMARY KEY,
            downtime_id INTEGER NOT NULL REFERENCES kpi_downtimes(id) ON DELETE CASCADE,
            area_id     INTEGER REFERENCES kpi_areas(id),
            station_id  INTEGER REFERENCES kpi_stations(id),
            reason_id   INTEGER NOT NULL REFERENCES kpi_reasons(id),
            minutes     NUMERIC(6, 1) NOT NULL CHECK (minutes > 0),
            note        TEXT NOT NULL DEFAULT '',
            created_by  INTEGER,
            created_by_name TEXT NOT NULL DEFAULT '',
            updated_by_name TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # ответственный (production / oto / ito / other / none = не назначен); по умолчанию «none»: отдел выбирает человек
    await pool.execute("ALTER TABLE kpi_downtime_items ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'none'")
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_downtime_items_dt ON kpi_downtime_items (downtime_id)")
    # журнал смены ответственного: кто, когда, было, стало (при создании строки «было» пусто)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_items_owner_log (
            id BIGSERIAL PRIMARY KEY, item_id INTEGER NOT NULL, downtime_id INTEGER NOT NULL, old_owner TEXT, new_owner TEXT NOT NULL,
            changed_by INTEGER, changed_by_name TEXT NOT NULL DEFAULT '', changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # журнал удалённых строк описания: удаление не стирает историю (кто, когда, что было)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_items_log (
            id BIGSERIAL PRIMARY KEY, item_id INTEGER, downtime_id INTEGER NOT NULL, area_id INTEGER, station_id INTEGER, reason_id INTEGER,
            minutes NUMERIC(6,1), note TEXT NOT NULL DEFAULT '', created_by INTEGER, created_by_name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ, deleted_by INTEGER, deleted_by_name TEXT NOT NULL DEFAULT '', deleted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("ALTER TABLE kpi_downtime_items_log ADD COLUMN IF NOT EXISTS owner TEXT")
    # ответ отдела ОТО / ИТО на ответственность (шаг 2 T-15): один ответ на строку описания, его можно изменить в любой день
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_responses (
            id         SERIAL PRIMARY KEY,
            item_id    INTEGER NOT NULL UNIQUE REFERENCES kpi_downtime_items(id) ON DELETE CASCADE,
            department TEXT NOT NULL CHECK (department IN ('oto', 'ito')),
            answer     TEXT NOT NULL CHECK (answer IN ('agree', 'partial', 'disagree')),
            minutes    NUMERIC(6, 1) CHECK (minutes IS NULL OR minutes > 0),
            comment    TEXT NOT NULL DEFAULT '',
            user_id    INTEGER,
            user_name  TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (answer <> 'partial' OR minutes IS NOT NULL)
        )
        """
    )
    # журнал ответов: кто, когда, было, стало (create / update / delete); без внешнего ключа, чтобы история оставалась
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_responses_log (
            id BIGSERIAL PRIMARY KEY, item_id INTEGER NOT NULL, department TEXT NOT NULL, action TEXT NOT NULL,
            old_answer TEXT, old_minutes NUMERIC(6,1), old_comment TEXT,
            new_answer TEXT, new_minutes NUMERIC(6,1), new_comment TEXT,
            changed_by INTEGER, changed_by_name TEXT NOT NULL DEFAULT '', changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


class ItemBody(BaseModel):
    id: int | None = None
    area_id: int | None = None
    station_id: int | None = None
    reason_id: int
    minutes: float = Field(gt=0, le=999)
    note: str = Field(default="", max_length=500)
    mentions: list[int] = Field(default_factory=list, max_length=10)   # кого отметили через @ в описании
    owner: Literal["production", "oto", "ito", "other", "none"] | None = None  # ответственный; не указан: из причины (новая строка) или прежний


class ItemsBody(BaseModel):
    items: list[ItemBody] = Field(max_length=30)


def _num(x: Decimal | float | None) -> float:
    return float(x or 0)


async def _screen_scope(pool: asyncpg.Pool, screen_id: int) -> str | None:
    s = await pool.fetchrow("SELECT name, template, bindings FROM kpi_screens WHERE id = $1", screen_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    if s["template"] == "fl_counter":
        return "FL"
    if s["template"] == "manual_counter":
        b = json.loads(s["bindings"]) if isinstance(s["bindings"], str) else dict(s["bindings"] or {})
        return b.get("brand") if b.get("brand") in SCOPES else None
    return next((k for k in SCOPES if k.lower() == s["name"].strip().lower()), None)


def _author(last: str | None, role: str | None, fallback: str) -> str:
    """Кто описал причину: «Фамилия, должность» (должность по роли пользователя)."""
    name = last or (fallback or "").split(" ")[0]
    pos = ROLES.get(role or "", "")
    return f"{name}, {pos}" if name and pos else name


@router.get("/screens/{screen_id}/downtimes")
async def list_downtimes(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date")) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    scope = await _screen_scope(pool, screen_id)
    if day is None:
        # у экрана подсборки своей почасовой истории нет: сутки берём у Main Line бренда
        day = await pool.fetchval(
            "SELECT max(prod_date) FROM kpi_hourly WHERE screen_id = COALESCE("
            "(SELECT (bindings->>'main')::int FROM kpi_screens WHERE id = $1 AND template = 'manual_counter'), $1)",
            screen_id,
        )
    if day is None:
        return {"date": None, "scope": scope, "downtimes": [], "responsibility": None}
    rows = await pool.fetch(
        "SELECT id, idx, plan, fact, interval_min, minutes FROM kpi_downtimes "
        "WHERE screen_id = $1 AND prod_date = $2 AND source = 'plan' ORDER BY idx",
        screen_id, day,
    )
    items = await pool.fetch(
        "SELECT i.id, i.downtime_id, i.area_id, i.station_id, i.reason_id, i.minutes, i.note, i.owner, "
        "i.created_by, i.created_by_name, i.updated_by_name, i.created_at, i.updated_at, u.last_name AS author_last, u.role AS author_role, "
        + RESP_COLS + " FROM kpi_downtime_items i JOIN kpi_downtimes d ON d.id = i.downtime_id LEFT JOIN app_users u ON u.id = i.created_by "
        + RESP_JOIN + " WHERE d.screen_id = $1 AND d.prod_date = $2 ORDER BY i.id",
        screen_id, day,
    )
    # комментарии привязаны к строкам описания: считаем по строкам, а по простою берём сумму
    item_comments = {
        r["item_id"]: r["n"]
        for r in await pool.fetch(
            "SELECT c.item_id, count(*) AS n FROM kpi_downtime_comments c JOIN kpi_downtimes d ON d.id = c.downtime_id "
            "WHERE d.screen_id = $1 AND d.prod_date = $2 AND c.item_id IS NOT NULL AND c.deleted_at IS NULL GROUP BY c.item_id",
            screen_id, day,
        )
    }
    # последние комментарии каждой строки (до 5, от старых к новым): на экране Andon в колонке «Причина» каждый на своей строке
    recent_comments: dict[int, list[dict[str, Any]]] = {}
    for r in await pool.fetch(
        "SELECT * FROM (SELECT c.item_id, c.created_at, c.id, c.text, c.user_name, c.is_oto, u.last_name AS author_last, u.role AS author_role, "
        "row_number() OVER (PARTITION BY c.item_id ORDER BY c.created_at DESC, c.id DESC) AS rn "
        "FROM kpi_downtime_comments c LEFT JOIN app_users u ON u.id = c.user_id "
        "JOIN kpi_downtimes d ON d.id = c.downtime_id "
        "WHERE d.screen_id = $1 AND d.prod_date = $2 AND c.item_id IS NOT NULL AND c.deleted_at IS NULL) t "
        "WHERE rn <= 5 ORDER BY item_id, created_at, id",
        screen_id, day,
    ):
        recent_comments.setdefault(r["item_id"], []).append(
            {"text": r["text"], "user_name": r["user_name"], "is_oto": r["is_oto"],
             "author": _author(r["author_last"], r["author_role"], r["user_name"])})
    last_comment = {k: v[-1] for k, v in recent_comments.items()}
    by_dt: dict[int, list[dict[str, Any]]] = {}
    for it in items:
        by_dt.setdefault(it["downtime_id"], []).append(
            {**{k: it[k] for k in ("id", "area_id", "station_id", "reason_id", "note", "created_by", "created_by_name", "updated_by_name")},
             "minutes": _num(it["minutes"]), "created_at": it["created_at"], "updated_at": it["updated_at"],
             "owner": it["owner"], "owner_status": owner_status(it["owner"], it["resp_answer"]), "owner_mark": owner_mark(it["owner"], it["resp_answer"]), "response": response_view(it),
             "comments": item_comments.get(it["id"], 0), "last_comment": last_comment.get(it["id"]),
             "recent_comments": recent_comments.get(it["id"], []),
             "author": _author(it["author_last"], it["author_role"], it["created_by_name"])}
        )
    # день без выпуска (закрытый, факт 0, без отметки «простой весь день»): автоматический простой не считается,
    # остаются только строки, которые описали люди (описал, значит считается); «не описано» нет
    st = await dayflags.day_state(pool, screen_id, day)
    no_output = st is not None and st["state"] == dayflags.NO_OUTPUT
    out = []
    for r in rows:
        its = by_dt.get(r["id"], [])
        described = round(sum(i["minutes"] for i in its), 1)
        if no_output:
            if not its:
                continue
            minutes = described
        else:
            minutes = _num(r["minutes"])
            if minutes <= 0 and not its:
                continue
        out.append({
            "id": r["id"], "idx": r["idx"], "plan": r["plan"], "fact": r["fact"], "interval_min": r["interval_min"],
            "minutes": minutes, "described": described, "items": its,
            "comments": sum(i["comments"] for i in its),
        })
    from responsibility import responsibility_totals   # итоги по отделам считает одна функция; здесь только отдаём их вместе со списком дня
    return {"date": day.isoformat(), "scope": scope, "downtimes": out, "no_output": no_output,
            "responsibility": await responsibility_totals(pool, screen_id, day, day)}


def _screen_path(template: str, name: str, bindings: Any) -> tuple[str, str]:
    """Бренд (или FL) и участок для подписи экрана: «GWM › Main Line»."""
    b = json.loads(bindings) if isinstance(bindings, str) else dict(bindings or {})
    if template == "fl_counter":
        return "FL", name.removeprefix("FL").strip() or name
    if template == "manual_counter":
        return b.get("brand") or name, b.get("area") or ""
    return name, "Main Line"


@router.get("/downtimes-list")
async def downtimes_list(
    request: Request,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    """Все простои за период одним списком: одна строка на каждую причину (плюс строка «не описано» для остатка)."""
    pool: asyncpg.Pool = request.app.state.pool
    today = await pool.fetchval("SELECT max(prod_date) FROM kpi_hourly")
    if today is None:
        return {"today": None, "rows": []}
    d1, d2 = date_from or today, date_to or today
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > 92:
        raise HTTPException(status_code=422, detail="Период не должен быть больше трёх месяцев")
    rows = await pool.fetch(
        """
        SELECT d.id AS downtime_id, d.screen_id, s.name AS screen_name, s.template, s.bindings, d.prod_date, d.idx,
               d.plan, d.fact, d.minutes, h.start_min, h.end_min,
               i.id AS item_id, i.minutes AS item_minutes, i.note, i.owner, i.created_by, i.created_by_name, i.updated_by_name,
               i.created_at, i.updated_at, ar.name AS area_name, st.name AS station_name, r.id AS reason_id, r.name AS reason_name,
               (SELECT count(*) FROM kpi_downtime_comments c WHERE c.item_id = i.id AND c.deleted_at IS NULL) AS comments, """ + RESP_COLS + """
        FROM kpi_downtimes d
        JOIN kpi_screens s ON s.id = d.screen_id
        LEFT JOIN kpi_hourly h ON h.prod_date = d.prod_date AND h.idx = d.idx AND h.screen_id = COALESCE(
            CASE WHEN s.template = 'manual_counter' THEN (s.bindings->>'main')::int END, d.screen_id)
        LEFT JOIN kpi_downtime_items i ON i.downtime_id = d.id
        """ + RESP_JOIN + """
        LEFT JOIN kpi_areas ar ON ar.id = i.area_id
        LEFT JOIN kpi_stations st ON st.id = i.station_id
        LEFT JOIN kpi_reasons r ON r.id = i.reason_id
        WHERE d.prod_date BETWEEN $1 AND $2 AND d.source = 'plan' AND (d.minutes > 0 OR i.id IS NOT NULL)
        ORDER BY d.prod_date DESC, COALESCE(h.start_min, 0), s.sort_order, s.id, d.idx, i.id
        """,
        d1, d2,
    )
    groups: dict[int, list[asyncpg.Record]] = {}
    for r in rows:
        groups.setdefault(r["downtime_id"], []).append(r)
    skip = await dayflags.no_output_days(pool, d1, d2)   # дни без выпуска: автоматический простой не считается
    out: list[dict[str, Any]] = []
    for recs in groups.values():
        head = recs[0]
        group, label = _screen_path(head["template"], head["screen_name"], head["bindings"])
        total = _num(head["minutes"])
        if (head["screen_id"], head["prod_date"]) in skip:
            total = round(sum(_num(r["item_minutes"]) for r in recs if r["item_id"] is not None), 1)   # остаются только описанные людьми строки
            if not total:
                continue
        base = {
            "downtime_id": head["downtime_id"], "screen_id": head["screen_id"], "group": group, "label": label,
            "date": head["prod_date"].isoformat(), "idx": head["idx"], "start_min": head["start_min"], "end_min": head["end_min"],
            "plan": head["plan"], "fact": head["fact"], "minutes": total,
        }
        described = 0.0
        first = True
        for r in recs:
            if r["item_id"] is None:
                continue
            described += _num(r["item_minutes"])
            out.append({**base, "kind": "item", "first": first, "item_id": r["item_id"], "item_minutes": _num(r["item_minutes"]),
                        "area": r["area_name"], "station": r["station_name"], "reason_id": r["reason_id"], "reason": r["reason_name"],
                        "note": r["note"], "owner": r["owner"], "owner_status": owner_status(r["owner"], r["resp_answer"]), "owner_mark": owner_mark(r["owner"], r["resp_answer"]),
                        "response": response_view(r),
                        "created_by": r["created_by"], "created_by_name": r["created_by_name"],
                        "updated_by_name": r["updated_by_name"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                        "comments": r["comments"]})
            first = False
        rest = round(total - described, 1)
        if rest > 0.5:
            out.append({**base, "kind": "rest", "first": first, "item_id": None, "item_minutes": rest, "comments": 0})
    from responsibility import responsibility_all
    return {"today": today.isoformat(), "from": d1.isoformat(), "to": d2.isoformat(), "rows": out,
            "responsibility": await responsibility_all(pool, d1, d2)}


@router.get("/downtimes/{downtime_id}")
async def get_downtime(downtime_id: int, request: Request) -> dict[str, Any]:
    """Один простой с экраном и временем интервала: нужен, чтобы открыть его из уведомления."""
    pool: asyncpg.Pool = request.app.state.pool
    dt = await pool.fetchrow("SELECT screen_id, prod_date, idx FROM kpi_downtimes WHERE id = $1", downtime_id)
    if dt is None:
        raise HTTPException(status_code=404, detail="Простой не найден")
    data = await list_downtimes(dt["screen_id"], request, dt["prod_date"])
    found = next((d for d in data["downtimes"] if d["id"] == downtime_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Простой не найден")
    iv = await pool.fetchrow(
        "SELECT h.start_min, h.end_min FROM kpi_hourly h WHERE h.prod_date = $2 AND h.idx = $3 AND h.screen_id = COALESCE("
        "(SELECT (bindings->>'main')::int FROM kpi_screens WHERE id = $1 AND template = 'manual_counter'), $1)",
        dt["screen_id"], dt["prod_date"], dt["idx"],
    )
    return {"screen_id": dt["screen_id"], "date": data["date"], "scope": data["scope"], "downtime": found,
            "start_min": iv["start_min"] if iv else None, "end_min": iv["end_min"] if iv else None}


@router.put("/downtimes/{downtime_id}/items")
async def save_items(
    downtime_id: int, body: ItemsBody, request: Request, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    dt = await pool.fetchrow("SELECT id, screen_id, minutes FROM kpi_downtimes WHERE id = $1", downtime_id)
    if dt is None:
        raise HTTPException(status_code=404, detail="Простой не найден")
    scope = await _screen_scope(pool, dt["screen_id"])

    reasons = {r["id"]: r for r in await pool.fetch("SELECT id, name, is_active, owner FROM kpi_reasons")}
    areas = {a["id"]: a for a in await pool.fetch("SELECT id, scope, is_active FROM kpi_areas")}
    stations = {s["id"]: s for s in await pool.fetch("SELECT id, area_id, name, is_active FROM kpi_stations")}
    existing = {r["id"]: r for r in await pool.fetch(
        "SELECT id, reason_id, area_id, station_id, minutes, note, owner, created_by, created_by_name FROM kpi_downtime_items WHERE downtime_id = $1", downtime_id)}

    total = 0.0
    for n, it in enumerate(body.items, 1):
        reason = reasons.get(it.reason_id)
        if reason is None:
            raise HTTPException(status_code=422, detail=f"Строка {n}: выберите причину из списка")
        # архивные значения нельзя выбрать заново, но уже сохранённые строки остаются как есть
        prev = existing.get(it.id) if it.id else None
        if not reason["is_active"] and not (prev and prev["reason_id"] == it.reason_id):
            raise HTTPException(status_code=422, detail=f"Строка {n}: причина «{reason['name']}» в архиве")
        if it.station_id is not None:
            st = stations.get(it.station_id)
            if st is None:
                raise HTTPException(status_code=422, detail=f"Строка {n}: станция не найдена")
            if it.area_id is not None and st["area_id"] != it.area_id:
                raise HTTPException(status_code=422, detail=f"Строка {n}: станция не относится к выбранному участку")
            it.area_id = st["area_id"]
        if it.area_id is not None:
            ar = areas.get(it.area_id)
            if ar is None or (scope is not None and ar["scope"] != scope):
                raise HTTPException(status_code=422, detail=f"Строка {n}: участок не относится к этому экрану")
        other = reason["name"] == OTHER or (it.station_id is not None and stations[it.station_id]["name"] == OTHER)
        if other and not it.note.strip():
            raise HTTPException(status_code=422, detail=f"Строка {n}: для «{OTHER}» опишите причину текстом")
        # ответственный: указан в строке, иначе прежний (правка) или из причины (новая строка)
        if it.owner is None:
            it.owner = prev["owner"] if prev else reason["owner"]
        if it.owner == "other" and not it.note.strip():
            raise HTTPException(status_code=422, detail=f"Строка {n}: для ответственного «{OWNERS['other']}» (внешние причины) опишите причину текстом")
        total += it.minutes
    if total > _num(dt["minutes"]) + 1:
        raise HTTPException(status_code=422, detail=f"Сумма строк ({total:g} мин) больше времени простоя ({_num(dt['minutes']):g} мин)")

    # Строку описания меняет и удаляет только её автор (и администратор с начальниками); остальные могут её только комментировать
    def can_touch(old: asyncpg.Record) -> bool:
        return user["role"] in MODERATORS or old["created_by"] == user["id"]

    def can_delete(old: asyncpg.Record) -> bool:
        return user["role"] == "admin" or old["created_by"] == user["id"]

    submitted = {it.id for it in body.items if it.id in existing}
    for it in body.items:
        old = existing.get(it.id) if it.id else None
        if old is not None and not can_touch(old):
            same = (it.area_id == old["area_id"] and it.station_id == old["station_id"] and it.reason_id == old["reason_id"]
                    and abs(it.minutes - _num(old["minutes"])) < 0.05 and it.note.strip() == old["note"] and it.owner == old["owner"])
            if not same:
                raise HTTPException(status_code=403, detail=f"Строку описал(а) {old['created_by_name']}: изменить её может только автор. Вы можете её прокомментировать")
    for old_id, old in existing.items():
        if old_id not in submitted and not can_delete(old):
            raise HTTPException(status_code=403, detail=f"Строку описал(а) {old['created_by_name']}: удалить её могут только автор и администратор")

    uname = user["full_name"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            keep = {it.id for it in body.items if it.id in existing}
            for old_id in existing:
                if old_id not in keep:
                    await conn.execute(
                        "INSERT INTO kpi_downtime_items_log (item_id, downtime_id, area_id, station_id, reason_id, minutes, note, owner, created_by, created_by_name, created_at, deleted_by, deleted_by_name) "
                        "SELECT id, downtime_id, area_id, station_id, reason_id, minutes, note, owner, created_by, created_by_name, created_at, $2, $3 FROM kpi_downtime_items WHERE id = $1",
                        old_id, user["id"], uname)
                    await conn.execute("DELETE FROM kpi_downtime_items WHERE id = $1", old_id)
            for it in body.items:
                note = it.note.strip()
                if it.id in existing:
                    await conn.execute(
                        "UPDATE kpi_downtime_items SET area_id=$2, station_id=$3, reason_id=$4, minutes=$5, note=$6, owner=$8, "
                        "updated_by_name=$7, updated_at=now() WHERE id=$1 AND (area_id, station_id, reason_id, minutes, note, owner) "
                        "IS DISTINCT FROM ($2, $3, $4, $5::numeric, $6, $8)",
                        it.id, it.area_id, it.station_id, it.reason_id, round(it.minutes, 1), note, uname, it.owner,
                    )
                    item_id, old_owner = it.id, existing[it.id]["owner"]
                else:
                    item_id, old_owner = await conn.fetchval(
                        "INSERT INTO kpi_downtime_items (downtime_id, area_id, station_id, reason_id, minutes, note, "
                        "created_by, created_by_name, updated_by_name, owner) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8,$9) RETURNING id",
                        downtime_id, it.area_id, it.station_id, it.reason_id, round(it.minutes, 1), note, user["id"], uname, it.owner,
                    ), None
                # журнал ответственного: кто, когда, было, стало (только при создании строки и при смене)
                if old_owner != it.owner:
                    await conn.execute(
                        "INSERT INTO kpi_downtime_items_owner_log (item_id, downtime_id, old_owner, new_owner, changed_by, changed_by_name) "
                        "VALUES ($1,$2,$3,$4,$5,$6)", item_id, downtime_id, old_owner, it.owner, user["id"], uname)
                # @ в описании: отмеченные коллеги получают то же уведомление, что и при упоминании в комментарии
                prev_note = existing[it.id]["note"] if it.id in existing else None
                if it.mentions and note and note != prev_note:
                    ids = [r["id"] for r in await conn.fetch(
                        "SELECT id FROM app_users WHERE id = ANY($1::int[]) AND is_active AND status = 'active' AND id <> $2",
                        list(dict.fromkeys(it.mentions)), user["id"])]
                    snippet = " ".join(note.split())[:140]
                    for uid in ids:
                        await conn.execute(
                            "INSERT INTO kpi_notifications (user_id, kind, downtime_id, comment_id, from_name, snippet) VALUES ($1,'mention',$2,NULL,$3,$4)",
                            uid, downtime_id, uname, snippet)
    return {"status": "ok", "described": round(total, 1)}
