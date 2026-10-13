"""Дни без выпуска и отметка «Простой весь день (поломка линии)».

Правило заказчика: если за закрытый день на линии не произведено ни одного кузова, это выходной или нет плана производства,
и такое время не идёт в статистику простоев. Исключение: пользователь отметил, что линия стояла из-за поломки.
Единая точка правила: is_no_output_day() и day_states(); их вызывают все места расчёта простоя и «не описано»
(Andon и его архив, «Простои», «Графики», «Отчёты» за день и период, Excel, автосохранение)."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import ROLES, require_editor

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))

NO_OUTPUT = "no_output"            # закрытый день без выпуска и без отметки: простой не считается
FULL_DOWNTIME = "full_downtime"    # закрытый день без выпуска, но отмечен «простой весь день»: простой считается как обычно
NO_OUTPUT_LABEL = "нет выпуска"
FULL_DOWNTIME_LABEL = "Простой весь день (поломка линии)"
NO_OUTPUT_HINT = ("За день не произведено ни одного кузова: выходной или нет плана, простой не считается. Если линия простаивала из-за поломки, "
                  "отметьте это кнопкой «Простой весь день (поломка линии)» в «Отчётах»")
FLAG_HINT = ("Для дня без выпуска: отметьте, если линия стояла из-за поломки. Тогда время простоя за этот день пойдёт в статистику. "
             "Снять отметку может автор и администратор")

router = APIRouter(prefix="/api/kpi")


async def ensure_dayflags_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_day_flags (
            screen_id     INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
            prod_date     DATE    NOT NULL,
            full_downtime BOOLEAN NOT NULL DEFAULT TRUE,
            set_by        INTEGER,
            set_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (screen_id, prod_date)
        )
        """
    )
    # журнал не связан внешним ключом с экраном: удаление отметки и даже экрана историю не стирает
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_day_flags_log (
            id            BIGSERIAL PRIMARY KEY,
            screen_id     INTEGER NOT NULL,
            prod_date     DATE    NOT NULL,
            old_value     BOOLEAN,
            new_value     BOOLEAN,
            user_id       INTEGER,
            user_name     TEXT NOT NULL DEFAULT '',
            changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_day_flags_log_screen ON kpi_day_flags_log (screen_id, prod_date)")


def plant_today() -> date:
    """Сегодняшняя дата по времени сервера (UTC + KPI_UTC_OFFSET_HOURS), как plantClock на экранах."""
    return (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).date()


def is_no_output_day(prod_date: date, fact_total: int | None, flagged: bool, today: date | None = None) -> bool:
    """Единое правило: день без выпуска = ЗАКРЫТЫЙ производственный день (строго раньше сегодняшнего) с суммарным фактом 0
    и без отметки «простой весь день». fact_total=None означает «фактических данных нет» (правило не применяется)."""
    if fact_total is None or flagged:
        return False
    return prod_date < (today or plant_today()) and fact_total == 0


def _author(last: str | None, role: str | None) -> str:
    pos = ROLES.get(role or "", "")
    return f"{last}, {pos}" if last and pos else (last or "—")


def set_at_text(ts: datetime | None) -> str:
    """Время отметки «ДД.ММ ЧЧ:ММ» по времени сервера."""
    if ts is None:
        return ""
    return (ts + timedelta(hours=UTC_OFFSET_HOURS)).strftime("%d.%m %H:%M")


async def day_facts(pool: asyncpg.Pool, d1: date, d2: date, screen_id: int | None = None) -> dict[tuple[int, date], int]:
    """Суммарный факт по интервалам за каждый день: {(экран, день): факт}. Main Line и Finish Line берут факт из kpi_hourly,
    подсборки из ручного ввода (только введённые значения; пока ничего не введено, дня в словаре нет и правило не применяется)."""
    sel = " AND screen_id = $3" if screen_id is not None else ""
    args: list[Any] = [d1, d2] + ([screen_id] if screen_id is not None else [])
    out: dict[tuple[int, date], int] = {}
    for r in await pool.fetch(
        f"SELECT screen_id, prod_date, COALESCE(sum(fact), 0)::int AS f FROM kpi_hourly WHERE prod_date BETWEEN $1 AND $2{sel} GROUP BY screen_id, prod_date", *args):
        out[(r["screen_id"], r["prod_date"])] = r["f"]
    for r in await pool.fetch(
        f"SELECT screen_id, prod_date, sum(fact)::int AS f FROM kpi_manual_hourly WHERE fact IS NOT NULL AND prod_date BETWEEN $1 AND $2{sel} "
        "GROUP BY screen_id, prod_date", *args):
        out[(r["screen_id"], r["prod_date"])] = r["f"]
    return out


async def _flags(pool: asyncpg.Pool, d1: date, d2: date, screen_id: int | None) -> dict[tuple[int, date], dict[str, Any]]:
    sel = " AND f.screen_id = $3" if screen_id is not None else ""
    args: list[Any] = [d1, d2] + ([screen_id] if screen_id is not None else [])
    rows = await pool.fetch(
        "SELECT f.screen_id, f.prod_date, f.full_downtime, f.set_by, f.set_at, u.last_name, u.role FROM kpi_day_flags f "
        f"LEFT JOIN app_users u ON u.id = f.set_by WHERE f.prod_date BETWEEN $1 AND $2{sel}", *args)
    return {(r["screen_id"], r["prod_date"]): {
        "full_downtime": r["full_downtime"], "set_by": r["set_by"], "set_by_name": _author(r["last_name"], r["role"]),
        "set_at": r["set_at"].isoformat() if r["set_at"] else None, "set_at_text": set_at_text(r["set_at"])} for r in rows}


async def day_states(pool: asyncpg.Pool, d1: date, d2: date, screen_id: int | None = None) -> dict[tuple[int, date], dict[str, Any]]:
    """Закрытые дни без выпуска за период (все экраны или один): {(экран, день): {"state": NO_OUTPUT | FULL_DOWNTIME, "flag": отметка или None}}.
    Дней с выпуском и текущего дня в словаре нет. ЕДИНАЯ точка, откуда берут правило все расчёты простоя."""
    facts = await day_facts(pool, d1, d2, screen_id)
    flags = await _flags(pool, d1, d2, screen_id)
    today = plant_today()
    out: dict[tuple[int, date], dict[str, Any]] = {}
    for key, total in facts.items():
        if key[1] >= today or total != 0:
            continue
        fl = flags.get(key)
        flagged = bool(fl and fl["full_downtime"])
        out[key] = {"state": NO_OUTPUT if is_no_output_day(key[1], total, flagged, today) else FULL_DOWNTIME, "flag": fl if flagged else None}
    return out


async def no_output_days(pool: asyncpg.Pool, d1: date, d2: date, screen_id: int | None = None) -> set[tuple[int, date]]:
    """Дни без выпуска, простой которых не считается (без дней с отметкой «простой весь день»)."""
    return {k for k, v in (await day_states(pool, d1, d2, screen_id)).items() if v["state"] == NO_OUTPUT}


async def day_state(pool: asyncpg.Pool, screen_id: int, day: date) -> dict[str, Any] | None:
    """Состояние одного дня экрана или None (день с выпуском, текущий или без данных)."""
    return (await day_states(pool, day, day, screen_id)).get((screen_id, day))


def day_note(state: dict[str, Any] | None) -> str | None:
    """Подпись под заголовком отчёта (экран и Excel одинаково): «нет выпуска» или «Простой весь день (поломка линии) · отмечено: ...»."""
    if state is None:
        return None
    if state["state"] == NO_OUTPUT:
        return f"{NO_OUTPUT_LABEL}: за день не произведено ни одного кузова, простой не считается"
    fl = state["flag"] or {}
    return f"{FULL_DOWNTIME_LABEL} · отмечено: {fl.get('set_by_name', '—')} · {fl.get('set_at_text', '')}".rstrip(" ·")


# ---------------------------------------------------------------- API

@router.get("/day-flags")
async def get_day_flags(request: Request, screen_id: int = Query(), date_from: date = Query(alias="from"), date_to: date = Query(alias="to")) -> dict[str, Any]:
    """Закрытые дни без выпуска по линии за период и отметки «простой весь день»; читают все без входа."""
    if (date_to - date_from).days > 400 or date_to < date_from:
        raise HTTPException(status_code=400, detail="Неверный период")
    pool: asyncpg.Pool = request.app.state.pool
    st = await day_states(pool, date_from, date_to, screen_id)
    return {"today": plant_today().isoformat(), "days": [
        {"date": d.isoformat(), "state": v["state"], "flag": v["flag"]} for (_, d), v in sorted(st.items(), key=lambda kv: kv[0][1])]}


async def _check_flaggable(pool: asyncpg.Pool, screen_id: int, day: date) -> None:
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    if day >= plant_today():
        raise HTTPException(status_code=409, detail="Отметить можно только закрытый день: сегодняшний день ещё не закончился")
    total = (await day_facts(pool, day, day, screen_id)).get((screen_id, day))
    if total is None:
        raise HTTPException(status_code=409, detail="За этот день нет данных по линии")
    if total != 0:
        raise HTTPException(status_code=409, detail="За этот день был выпуск: отметка нужна только для дня без выпуска")


@router.put("/screens/{screen_id}/day-flag/{day}")
async def put_day_flag(screen_id: int, day: date, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    """Отметить закрытый день без выпуска как «простой весь день (поломка линии)»: простой дня пойдёт в статистику."""
    pool: asyncpg.Pool = request.app.state.pool
    await _check_flaggable(pool, screen_id, day)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT full_downtime FROM kpi_day_flags WHERE screen_id = $1 AND prod_date = $2 FOR UPDATE", screen_id, day)
            if old is not None and old["full_downtime"]:
                return {"status": "unchanged"}
            await conn.execute(
                "INSERT INTO kpi_day_flags (screen_id, prod_date, full_downtime, set_by) VALUES ($1, $2, TRUE, $3) "
                "ON CONFLICT (screen_id, prod_date) DO UPDATE SET full_downtime = TRUE, set_by = EXCLUDED.set_by, set_at = now()",
                screen_id, day, user["id"])
            await conn.execute(
                "INSERT INTO kpi_day_flags_log (screen_id, prod_date, old_value, new_value, user_id, user_name) VALUES ($1, $2, $3, TRUE, $4, $5)",
                screen_id, day, None if old is None else old["full_downtime"], user["id"], user["full_name"])
    return {"status": "ok"}


@router.delete("/screens/{screen_id}/day-flag/{day}")
async def delete_day_flag(screen_id: int, day: date, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    """Снять отметку: может тот, кто её поставил, и администратор. Строка журнала остаётся."""
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT full_downtime, set_by FROM kpi_day_flags WHERE screen_id = $1 AND prod_date = $2 FOR UPDATE", screen_id, day)
            if old is None:
                raise HTTPException(status_code=404, detail="Отметки на этот день нет")
            if user["role"] != "admin" and old["set_by"] != user["id"]:
                raise HTTPException(status_code=403, detail="Снять отметку может тот, кто её поставил, или администратор")
            await conn.execute("DELETE FROM kpi_day_flags WHERE screen_id = $1 AND prod_date = $2", screen_id, day)
            await conn.execute(
                "INSERT INTO kpi_day_flags_log (screen_id, prod_date, old_value, new_value, user_id, user_name) VALUES ($1, $2, $3, NULL, $4, $5)",
                screen_id, day, old["full_downtime"], user["id"], user["full_name"])
    return {"status": "ok"}
