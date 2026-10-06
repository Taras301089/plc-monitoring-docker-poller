"""Ширины столбцов таблиц: личные для вошедшего пользователя и общие (по умолчанию для всех, в том числе для табло без входа)."""
from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import current_user, require_admin, require_user

router = APIRouter(prefix="/api/ui")

KEY_RE = re.compile(r"^[a-z0-9_-]{1,60}$")


async def ensure_colwidths_schema(pool: asyncpg.Pool) -> None:
    # user_id = NULL: общая раскладка для всех; иначе личная раскладка пользователя
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_col_widths (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
            key        TEXT        NOT NULL,
            widths     JSONB       NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ui_col_widths ON ui_col_widths ((COALESCE(user_id, 0)), key)")


def _key(key: str) -> str:
    if not KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Недопустимое имя таблицы")
    return key


class WidthsBody(BaseModel):
    widths: list[float] = Field(min_length=1, max_length=60)
    shared: bool = False


@router.get("/col-widths")
async def get_widths(request: Request, user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    """Общие ширины видят все (в том числе без входа); личные только вошедший пользователь."""
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT user_id, key, widths FROM ui_col_widths WHERE user_id IS NULL OR user_id = $1",
        user["id"] if user else None,
    )
    out: dict[str, dict[str, Any]] = {"shared": {}, "mine": {}}
    for r in rows:
        out["shared" if r["user_id"] is None else "mine"][r["key"]] = json.loads(r["widths"])
    return out


@router.put("/col-widths/{key}")
async def put_widths(key: str, body: WidthsBody, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    key = _key(key)
    if any(not (0 < w <= 5000) for w in body.widths):
        raise HTTPException(status_code=422, detail="Недопустимая ширина столбца")
    if body.shared and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Общие ширины столбцов может задавать только администратор")
    owner = None if body.shared else user["id"]
    await request.app.state.pool.execute(
        """
        INSERT INTO ui_col_widths (user_id, key, widths) VALUES ($1, $2, $3::jsonb)
        ON CONFLICT ((COALESCE(user_id, 0)), key) DO UPDATE SET widths = EXCLUDED.widths, updated_at = now()
        """,
        owner, key, json.dumps(body.widths),
    )
    return {"status": "ok"}


@router.delete("/col-widths/{key}")
async def delete_widths(key: str, request: Request, shared: bool = False, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    key = _key(key)
    if shared:
        await require_admin(user)
    await request.app.state.pool.execute(
        "DELETE FROM ui_col_widths WHERE key = $1 AND user_id IS NOT DISTINCT FROM $2", key, None if shared else user["id"],
    )
    return {"status": "ok"}
