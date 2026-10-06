"""Активность пользователей: кто сейчас в системе и статистика входов (видит только администратор)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import DEPARTMENTS, ROLES, require_user

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
ONLINE_SEC = 120          # сигнал не старше этого времени = пользователь в сети
IDLE_SEC = 300            # без действий дольше этого = «неактивен»
BEAT_GAP_SEC = 120        # пауза между сигналами дольше этого не засчитывается как время работы
BEAT_CAP_SEC = 60         # один интервал между сигналами засчитывается не больше чем на минуту
VIEW_ROLES = {"admin"}

router = APIRouter(prefix="/api/activity")


async def ensure_activity_schema(pool: asyncpg.Pool) -> None:
    # Одна строка на пользователя: последний сигнал браузера и где он находится
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS app_presence (
            user_id     INTEGER PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
            last_beat   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_active TIMESTAMPTZ,
            tab         TEXT,
            screen_id   INTEGER
        )
        """
    )
    await pool.execute("ALTER TABLE app_presence ADD COLUMN IF NOT EXISTS beat_active BOOLEAN NOT NULL DEFAULT TRUE")
    # Время использования по дням (местная дата): «в сети» = вкладка открыта, «работал» = были действия мышью или клавиатурой
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS app_usage_daily (
            user_id    INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            day        DATE    NOT NULL,
            online_sec INTEGER NOT NULL DEFAULT 0,
            active_sec INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
        """
    )


class BeatBody(BaseModel):
    active: bool = True                           # были ли действия мышью или клавиатурой в последние минуты
    tab: str | None = Field(default=None, max_length=40)
    screen: int | None = None


def _require_viewer(user: dict[str, Any]) -> None:
    if user["role"] not in VIEW_ROLES:
        raise HTTPException(status_code=403, detail="Статистику активности видит только администратор")


@router.post("/beat")
async def beat(body: BeatBody, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    now = datetime.now(timezone.utc)
    day = (now + timedelta(hours=UTC_OFFSET_HOURS)).date()
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev = await conn.fetchrow("SELECT last_beat, beat_active FROM app_presence WHERE user_id = $1 FOR UPDATE", user["id"])
            add_online = add_active = 0
            if prev is not None:
                delta = (now - prev["last_beat"]).total_seconds()
                # интервал между сигналами засчитываем, если сигналы шли подряд (не было паузы дольше 2 минут)
                if 0 < delta <= BEAT_GAP_SEC:
                    add_online = round(min(delta, BEAT_CAP_SEC))
                    # «работал» — только если и прошлый, и этот сигнал были с действиями пользователя
                    if body.active and prev["beat_active"]:
                        add_active = add_online
            await conn.execute(
                """
                INSERT INTO app_presence (user_id, last_beat, last_active, tab, screen_id, beat_active)
                VALUES ($1, now(), CASE WHEN $2 THEN now() END, $3, $4, $2)
                ON CONFLICT (user_id) DO UPDATE SET
                    last_beat = now(),
                    last_active = CASE WHEN $2 THEN now() ELSE app_presence.last_active END,
                    tab = $3, screen_id = $4, beat_active = $2
                """,
                user["id"], body.active, body.tab, body.screen,
            )
            if add_online:
                await conn.execute(
                    "INSERT INTO app_usage_daily (user_id, day, online_sec, active_sec) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (user_id, day) DO UPDATE SET online_sec = app_usage_daily.online_sec + EXCLUDED.online_sec, "
                    "active_sec = app_usage_daily.active_sec + EXCLUDED.active_sec",
                    user["id"], day, add_online, add_active,
                )
    return {"status": "ok"}


@router.get("/online")
async def online(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _require_viewer(user)
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT u.id, u.last_name, u.first_name, u.role, u.department, p.last_beat, p.last_active, p.tab, p.screen_id,
               EXTRACT(EPOCH FROM (now() - p.last_beat)) AS beat_age,
               EXTRACT(EPOCH FROM (now() - COALESCE(p.last_active, p.last_beat))) AS idle_age
        FROM app_presence p JOIN app_users u ON u.id = p.user_id
        WHERE u.is_active AND u.status = 'active' AND p.last_beat > now() - make_interval(secs => $1)
        ORDER BY u.last_name, u.first_name
        """,
        float(ONLINE_SEC),
    )
    return {
        "idle_sec": IDLE_SEC,
        "users": [
            {
                "id": r["id"], "name": f"{r['last_name']} {r['first_name']}", "role_name": ROLES.get(r["role"], r["role"]),
                "department_name": DEPARTMENTS.get(r["department"] or "", ""), "tab": r["tab"], "screen_id": r["screen_id"],
                "idle": r["idle_age"] > IDLE_SEC, "idle_sec": int(r["idle_age"]), "last_beat": r["last_beat"],
            }
            for r in rows
        ],
    }


def _period_start(period: str) -> datetime:
    """Начало периода по местному времени, переведённое в UTC: сегодня / 7 дней / 30 дней."""
    local_now = datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {"today": 0, "7": 6, "30": 29}[period]
    return (midnight - timedelta(days=days) - timedelta(hours=UTC_OFFSET_HOURS))


def _period_first_day(period: str):
    """Первый день периода по местной дате (для таблицы времени по дням)."""
    local_today = (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).date()
    return local_today - timedelta(days={"today": 0, "7": 6, "30": 29}[period])


@router.get("/days")
async def days(
    request: Request,
    period: Literal["today", "7", "30"] = Query(default="7"),
    user_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Статистика по дням: только администратор."""
    _require_viewer(user)
    target = user["id"] if user_id is None else user_id
    pool: asyncpg.Pool = request.app.state.pool
    who = await pool.fetchrow("SELECT last_name, first_name FROM app_users WHERE id = $1", target)
    if who is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    first = _period_first_day(period)
    rows = await pool.fetch(
        """
        SELECT d.day,
               (SELECT count(*) FROM app_login_events e WHERE e.user_id = $1
                  AND ((e.ts AT TIME ZONE 'UTC') + make_interval(hours => $3::int))::date = d.day) AS logins,
               d.online_sec, d.active_sec
        FROM app_usage_daily d WHERE d.user_id = $1 AND d.day >= $2 ORDER BY d.day DESC
        """,
        target, first, int(UTC_OFFSET_HOURS),
    )
    items = [{"day": r["day"].isoformat(), "logins": r["logins"], "online_sec": r["online_sec"], "active_sec": r["active_sec"]} for r in rows]
    return {
        "name": f"{who['last_name']} {who['first_name']}", "period": period, "days": items,
        "total": {"active_sec": sum(i["active_sec"] for i in items), "online_sec": sum(i["online_sec"] for i in items),
                  "logins": await pool.fetchval("SELECT count(*) FROM app_login_events WHERE user_id = $1 AND ts >= $2", target, _period_start(period))},
    }


@router.get("/stats")
async def stats(
    request: Request,
    period: Literal["today", "7", "30"] = Query(default="7"),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    _require_viewer(user)
    pool: asyncpg.Pool = request.app.state.pool
    start = _period_start(period)
    rows = await pool.fetch(
        """
        SELECT u.id, u.last_name, u.first_name, u.role, u.department,
               (SELECT count(*) FROM app_login_events e WHERE e.user_id = u.id AND e.ts >= $1) AS logins,
               (SELECT max(e.ts) FROM app_login_events e WHERE e.user_id = u.id) AS last_login,
               (SELECT COALESCE(sum(d.active_sec), 0) FROM app_usage_daily d WHERE d.user_id = u.id AND d.day >= $2) AS active_sec,
               (SELECT COALESCE(sum(d.online_sec), 0) FROM app_usage_daily d WHERE d.user_id = u.id AND d.day >= $2) AS online_sec,
               p.last_beat
        FROM app_users u LEFT JOIN app_presence p ON p.user_id = u.id
        WHERE u.is_active AND u.status = 'active'
        ORDER BY active_sec DESC, logins DESC, u.last_name, u.first_name
        """,
        start, _period_first_day(period),
    )
    return {
        "period": period, "since": start,
        "users": [
            {
                "id": r["id"], "name": f"{r['last_name']} {r['first_name']}", "role_name": ROLES.get(r["role"], r["role"]),
                "department_name": DEPARTMENTS.get(r["department"] or "", ""), "logins": r["logins"],
                "active_sec": int(r["active_sec"]), "online_sec": int(r["online_sec"]),
                "last_login": r["last_login"], "last_beat": r["last_beat"],
            }
            for r in rows
        ],
    }
