"""Бэкапы: список дампов, размер базы и прогноз, на сколько хватит места на диске (только для администратора)."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Request

from auth import require_admin

router = APIRouter(prefix="/api")

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
DUMP_RE = re.compile(r"^(hub|n8n)_(\d{4}-\d{2}-\d{2})\.dump$")
MIN_SPAN_DAYS = 7  # меньше этой истории прогноз не строится


def forecast(hub_points: list[tuple[date, int]], db_size: int, disk_free: int | None) -> dict[str, Any]:
    """Рост базы по размерам ежедневных дампов (пересчитан на размер базы) и запас места на диске."""
    pts = sorted(hub_points)
    result: dict[str, Any] = {"status": "no_data", "span_days": 0, "growth_per_day": None, "days_left": None}
    if len(pts) < 2 or disk_free is None:
        return result
    span = (pts[-1][0] - pts[0][0]).days
    result["span_days"] = span
    if span < MIN_SPAN_DAYS or pts[-1][1] <= 0:
        return result
    dump_growth = (pts[-1][1] - pts[0][1]) / span
    growth = max(0.0, dump_growth * db_size / pts[-1][1])
    result["growth_per_day"] = int(growth)
    if growth <= 0:
        result["status"] = "no_growth"
        return result
    result["status"] = "ok"
    result["days_left"] = int(disk_free / growth)
    return result


def _read_backups() -> dict[str, Any]:
    info: dict[str, Any] = {"folder_available": BACKUP_DIR.is_dir(), "files": [], "status": None}
    if not info["folder_available"]:
        return info
    for p in BACKUP_DIR.glob("*.dump"):
        st = p.stat()
        info["files"].append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    info["files"].sort(key=lambda f: f["mtime"], reverse=True)
    try:
        info["status"] = json.loads((BACKUP_DIR / "status.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        info["status"] = None
    return info


@router.get("/backups")
async def backups_info(request: Request, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    info = _read_backups()
    files = info["files"]
    status = info["status"] or {}

    db_size = await pool.fetchval("SELECT pg_database_size(current_database())")
    tables = await pool.fetch(
        "SELECT relname AS name, pg_total_relation_size(c.oid) AS size "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY size DESC LIMIT 10"
    )

    hub_points: list[tuple[date, int]] = []
    for f in files:
        m = DUMP_RE.match(f["name"])
        if m and m.group(1) == "hub":
            hub_points.append((date.fromisoformat(m.group(2)), f["size"]))
    dated = [f for f in files if DUMP_RE.match(f["name"])]
    last_ts = max((f["mtime"] for f in dated), default=None)

    return {
        "folder": status.get("folder") or str(BACKUP_DIR),
        "folder_available": info["folder_available"],
        "status_ok": status.get("ok") if status else None,
        "status_error": status.get("error") if status else None,
        "keep_days": status.get("keep_days"),
        "last_backup_age_hours": round((time.time() - last_ts) / 3600, 1) if last_ts else None,
        "files": files,
        "backups_total": sum(f["size"] for f in files),
        "disk_total": status.get("disk_total"),
        "disk_free": status.get("disk_free"),
        "db_size": db_size,
        "tables": [{"name": r["name"], "size": r["size"]} for r in tables],
        "forecast": forecast(hub_points, db_size, status.get("disk_free")),
    }
