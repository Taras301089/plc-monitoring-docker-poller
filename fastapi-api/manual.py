"""Экраны подсборок (Floor subassembly, Body side): план и факт по часам вводят вручную, сетка интервалов как у Main Line бренда."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from auth import require_editor

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
BRANDS = ["Chery", "Changan", "GWM"]
AREAS = ["Floor subassembly", "Body side"]

router = APIRouter(prefix="/api/kpi")


async def ensure_manual_schema(pool: asyncpg.Pool) -> None:
    # fact = NULL значит «ещё не введён»: простой по такому интервалу не создаётся
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_manual_hourly (
            screen_id       INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
            prod_date       DATE    NOT NULL,
            idx             INTEGER NOT NULL,
            plan            INTEGER NOT NULL DEFAULT 0,
            fact            INTEGER,
            updated_by_name TEXT NOT NULL DEFAULT '',
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (screen_id, prod_date, idx)
        )
        """
    )
    # Журнал: кто, когда и что изменил (план или факт)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_manual_log (
            id        BIGSERIAL PRIMARY KEY,
            ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
            screen_id INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
            prod_date DATE    NOT NULL,
            idx       INTEGER NOT NULL,
            field     TEXT    NOT NULL,
            old_value INTEGER,
            new_value INTEGER,
            user_id   INTEGER,
            user_name TEXT NOT NULL DEFAULT ''
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_manual_log_screen ON kpi_manual_log (screen_id, ts DESC)")


async def seed_manual_screens(pool: asyncpg.Pool) -> None:
    """Экраны подсборок создаются для каждого бренда, у которого есть экран Main Line (ПЛК берётся тот же, он нужен только как привязка)."""
    for brand in BRANDS:
        main = await pool.fetchrow(
            "SELECT id, plc_id, sort_order FROM kpi_screens WHERE template = 'body_counter' AND lower(name) = lower($1)", brand
        )
        if main is None:
            continue
        for n, area in enumerate(AREAS, 1):
            await pool.execute(
                "INSERT INTO kpi_screens (name, plc_id, template, db_name, bindings, sort_order) "
                "VALUES ($1, $2, 'manual_counter', '', $3::jsonb, $4) ON CONFLICT (plc_id, name) DO NOTHING",
                f"{brand} {area}", main["plc_id"],
                json.dumps({"brand": brand, "area": area, "main": str(main["id"])}), main["sort_order"] * 10 + n,
            )


class ManualValue(BaseModel):
    field: Literal["plan", "fact"]
    value: int | None = None


async def _manual_screen(pool: asyncpg.Pool, screen_id: int) -> dict[str, Any]:
    s = await pool.fetchrow("SELECT id, name, template, bindings FROM kpi_screens WHERE id = $1", screen_id)
    if s is None or s["template"] != "manual_counter":
        raise HTTPException(status_code=404, detail="Экран подсборки не найден")
    b = json.loads(s["bindings"]) if isinstance(s["bindings"], str) else dict(s["bindings"] or {})
    return {"id": s["id"], "name": s["name"], "main": int(b.get("main") or 0)}


async def _grid(pool: asyncpg.Pool, main_id: int) -> tuple[date | None, list[asyncpg.Record]]:
    day = await pool.fetchval("SELECT max(prod_date) FROM kpi_hourly WHERE screen_id = $1", main_id)
    if day is None:
        return None, []
    rows = await pool.fetch(
        "SELECT idx, start_min, end_min FROM kpi_hourly WHERE screen_id = $1 AND prod_date = $2 ORDER BY idx", main_id, day
    )
    return day, rows


@router.get("/screens/{screen_id}/manual")
async def get_manual(screen_id: int, request: Request) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    s = await _manual_screen(pool, screen_id)
    day, grid = await _grid(pool, s["main"])
    if day is None:
        return {"date": None, "now_min": 0, "intervals": []}
    vals = {r["idx"]: r for r in await pool.fetch(
        "SELECT idx, plan, fact, updated_by_name, updated_at FROM kpi_manual_hourly WHERE screen_id = $1 AND prod_date = $2", screen_id, day)}
    local = (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).replace(tzinfo=None)
    now_min = (local - datetime.combine(day, datetime.min.time())).total_seconds() / 60
    out = []
    for g in grid:
        v = vals.get(g["idx"])
        out.append({
            "idx": g["idx"], "start_min": g["start_min"], "end_min": g["end_min"],
            "plan": v["plan"] if v else 0, "fact": v["fact"] if v else None,
            "updated_by": v["updated_by_name"] if v else "", "updated_at": v["updated_at"] if v else None,
        })
    return {"date": day.isoformat(), "now_min": round(now_min, 1), "intervals": out}


@router.put("/screens/{screen_id}/manual/{idx}")
async def put_manual(
    screen_id: int, idx: int, body: ManualValue, request: Request, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    s = await _manual_screen(pool, screen_id)
    day, grid = await _grid(pool, s["main"])
    if day is None or idx not in {g["idx"] for g in grid}:
        raise HTTPException(status_code=404, detail="Такого интервала нет в сетке линии")
    value = body.value
    if value is not None and not 0 <= value <= 999:
        raise HTTPException(status_code=422, detail="Введите целое число от 0 до 999")
    if body.field == "plan" and value is None:
        value = 0
    uname = user["full_name"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT plan, fact FROM kpi_manual_hourly WHERE screen_id = $1 AND prod_date = $2 AND idx = $3 FOR UPDATE",
                screen_id, day, idx,
            )
            old_value = (old[body.field] if old else (0 if body.field == "plan" else None))
            if old_value == value and old is not None:
                return {"status": "unchanged"}
            col = body.field  # значение из Literal, подстановка безопасна
            await conn.execute(
                f"INSERT INTO kpi_manual_hourly (screen_id, prod_date, idx, {col}, updated_by_name) VALUES ($1, $2, $3, $4, $5) "
                f"ON CONFLICT (screen_id, prod_date, idx) DO UPDATE SET {col} = EXCLUDED.{col}, "
                f"updated_by_name = EXCLUDED.updated_by_name, updated_at = now()",
                screen_id, day, idx, value, uname,
            )
            await conn.execute(
                "INSERT INTO kpi_manual_log (screen_id, prod_date, idx, field, old_value, new_value, user_id, user_name) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                screen_id, day, idx, body.field, old_value, value, user["id"], uname,
            )
    return {"status": "ok"}


@router.get("/screens/{screen_id}/manual-log")
async def manual_log(screen_id: int, request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    pool: asyncpg.Pool = request.app.state.pool
    await _manual_screen(pool, screen_id)
    rows = await pool.fetch(
        "SELECT ts, prod_date, idx, field, old_value, new_value, user_name FROM kpi_manual_log "
        "WHERE screen_id = $1 ORDER BY ts DESC, id DESC LIMIT $2", screen_id, limit,
    )
    return [dict(r) for r in rows]
