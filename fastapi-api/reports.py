"""Автосохранение отчётов дня в папку на ПК с Docker: настройки по линиям, фоновый цикл и ручное сохранение."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from auth import require_admin
from downtime import _screen_path
from history import build_day_report
from period import build_period_report, month_bounds

log = logging.getLogger("reports")

UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/reports"))
REPORTS_HOST_DIR = os.getenv("REPORTS_HOST_DIR", str(REPORTS_DIR))
TICK_SEC = 60
DEFAULT_TIME = "17:00"
AFTER_SHIFT_MIN = 15      # предлагаемое время сохранения: конец последнего интервала плюс столько минут

router = APIRouter(prefix="/api/reports")


async def ensure_reports_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_report_settings (
            screen_id       INTEGER PRIMARY KEY REFERENCES kpi_screens(id) ON DELETE CASCADE,
            enabled         BOOLEAN NOT NULL DEFAULT FALSE,
            save_time       TEXT    NOT NULL DEFAULT '17:00',
            last_saved_date DATE,
            last_saved_at   TIMESTAMPTZ,
            last_error      TEXT
        )
        """
    )
    await pool.execute("ALTER TABLE kpi_report_settings ADD COLUMN IF NOT EXISTS last_month_saved TEXT")


def _local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


async def save_day(pool: asyncpg.Pool, screen_id: int, day: date) -> tuple[Path | None, int]:
    """Строит отчёт дня и кладёт его в «месяц/линия/файл.xlsx»; (путь, число интервалов). Без данных файл не создаётся."""
    rep = await build_day_report(pool, screen_id, day)
    if rep is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    data, folder, fname, n = rep
    if n == 0:
        return None, 0
    path = REPORTS_DIR / day.strftime("%Y-%m") / folder / fname
    await asyncio.to_thread(_write_file, path, data)
    return path, n


async def save_month(pool: asyncpg.Pool, screen_id: int, year: int, month: int) -> Path | None:
    """Отчёт за месяц с графиками в «месяц/линия/линия_месяц_ГГГГ-ММ.xlsx»; без данных за месяц файл не создаётся."""
    d1, d2 = month_bounds(year, month)
    rep = await build_period_report(pool, screen_id, d1, d2)
    if rep is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    data, folder, _, days = rep
    if days == 0:
        return None
    path = REPORTS_DIR / d1.strftime("%Y-%m") / folder / f"{folder}_месяц_{d1.strftime('%Y-%m')}.xlsx"
    await asyncio.to_thread(_write_file, path, data)
    return path


async def _tick_month(pool: asyncpg.Pool, today: date, hm: str) -> None:
    """В первый день месяца, в заданное время линии, сохраняет отчёт за прошлый месяц."""
    if today.day != 1:
        return
    prev = today - timedelta(days=1)
    tag = prev.strftime("%Y-%m")
    rows = await pool.fetch(
        "SELECT screen_id FROM kpi_report_settings WHERE enabled AND save_time <= $1 AND last_month_saved IS DISTINCT FROM $2",
        hm, tag,
    )
    for r in rows:
        sid = r["screen_id"]
        try:
            path = await save_month(pool, sid, prev.year, prev.month)
            if path:
                log.info("Месячный отчёт сохранён: %s", path)
            await pool.execute("UPDATE kpi_report_settings SET last_month_saved = $2 WHERE screen_id = $1", sid, tag)
        except Exception:
            log.exception("Не удалось сохранить месячный отчёт экрана %s", sid)


async def _tick(pool: asyncpg.Pool) -> None:
    now = _local_now()
    today, hm = now.date(), now.strftime("%H:%M")
    await _tick_month(pool, today, hm)
    rows = await pool.fetch(
        "SELECT screen_id, save_time FROM kpi_report_settings "
        "WHERE enabled AND last_saved_date IS DISTINCT FROM $1 AND save_time <= $2",
        today, hm,
    )
    for r in rows:
        sid = r["screen_id"]
        try:
            path, n = await save_day(pool, sid, today)
            err = None if n else "За день нет данных, файл не создан"
            if path:
                log.info("Отчёт сохранён: %s", path)
            await pool.execute(
                "UPDATE kpi_report_settings SET last_saved_date = $2, last_saved_at = CASE WHEN $3 THEN now() ELSE last_saved_at END, "
                "last_error = $4 WHERE screen_id = $1",
                sid, today, bool(n), err,
            )
        except Exception as ex:  # повторим на следующем тике
            log.exception("Не удалось сохранить отчёт экрана %s", sid)
            await pool.execute("UPDATE kpi_report_settings SET last_error = $2 WHERE screen_id = $1", sid, str(ex)[:300])


async def reports_loop(app: Any) -> None:
    while True:
        try:
            await _tick(app.state.pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Сбой цикла автосохранения отчётов")
        await asyncio.sleep(TICK_SEC)


def _hhmm_valid(s: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s))


@router.get("/settings")
async def get_settings(request: Request, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT s.id, s.name, s.template, s.bindings, r.enabled, r.save_time, r.last_saved_date, r.last_saved_at, r.last_error,
               (SELECT max(end_min) FROM kpi_schedule c WHERE c.screen_id = s.id AND c.active AND c.plan > 0) AS last_end
        FROM kpi_screens s LEFT JOIN kpi_report_settings r ON r.screen_id = s.id
        ORDER BY s.sort_order, s.id
        """
    )
    screens = []
    for r in rows:
        group, label = _screen_path(r["template"], r["name"], r["bindings"])
        end = r["last_end"]
        default_time = f"{((end + AFTER_SHIFT_MIN) // 60) % 24:02d}:{(end + AFTER_SHIFT_MIN) % 60:02d}" if end else DEFAULT_TIME
        screens.append({
            "id": r["id"], "name": f"{group} › {label}" if label else group,
            "enabled": bool(r["enabled"]), "save_time": r["save_time"] or default_time, "default_time": default_time,
            "last_saved_date": r["last_saved_date"].isoformat() if r["last_saved_date"] else None,
            "last_saved_at": r["last_saved_at"].isoformat() if r["last_saved_at"] else None,
            "last_error": r["last_error"],
        })
    return {"dir": REPORTS_HOST_DIR, "screens": screens}


class SettingBody(BaseModel):
    enabled: bool
    save_time: str


@router.put("/settings/{screen_id}")
async def put_setting(screen_id: int, body: SettingBody, request: Request, _: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    if not _hhmm_valid(body.save_time):
        raise HTTPException(status_code=400, detail="Время должно быть в формате ЧЧ:ММ")
    pool: asyncpg.Pool = request.app.state.pool
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    # при смене времени или повторном включении отчёт за сегодня может быть сохранён ещё раз
    await pool.execute(
        """
        INSERT INTO kpi_report_settings (screen_id, enabled, save_time) VALUES ($1, $2, $3)
        ON CONFLICT (screen_id) DO UPDATE SET enabled = EXCLUDED.enabled, save_time = EXCLUDED.save_time,
            last_saved_date = CASE WHEN kpi_report_settings.save_time <> EXCLUDED.save_time
                                     OR NOT kpi_report_settings.enabled THEN NULL ELSE kpi_report_settings.last_saved_date END
        """,
        screen_id, body.enabled, body.save_time,
    )
    return {"status": "ok"}


@router.post("/save-now/{screen_id}")
async def save_now(screen_id: int, request: Request, day: date | None = Query(default=None, alias="date"),
                   _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Сохранить отчёт выбранного дня (по умолчанию сегодняшнего) прямо сейчас."""
    pool: asyncpg.Pool = request.app.state.pool
    path, n = await save_day(pool, screen_id, day or _local_now().date())
    if not path:
        raise HTTPException(status_code=404, detail="За этот день нет данных, файл не создан")
    rel = path.relative_to(REPORTS_DIR)
    return {"file": str(Path(REPORTS_HOST_DIR) / rel), "intervals": n}
