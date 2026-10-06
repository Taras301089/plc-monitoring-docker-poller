"""Комментарии к простоям (с ответами и пометкой ОТО), упоминания через @ и уведомления внутри сайта."""
from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import DEPARTMENTS, ROLES, require_editor, require_user

# Кто может оставить комментарий «от ОТО» и удалять чужие комментарии
OTO_ROLES = {"admin", "chief", "area_head", "maintenance"}
MODERATOR_ROLES = {"admin", "chief", "area_head"}

router = APIRouter(prefix="/api/kpi")


async def ensure_comments_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_downtime_comments (
            id          SERIAL PRIMARY KEY,
            downtime_id INTEGER NOT NULL REFERENCES kpi_downtimes(id) ON DELETE CASCADE,
            parent_id   INTEGER REFERENCES kpi_downtime_comments(id) ON DELETE CASCADE,
            user_id     INTEGER,
            user_name   TEXT NOT NULL,
            department  TEXT,
            is_oto      BOOLEAN NOT NULL DEFAULT FALSE,
            oto_request BOOLEAN NOT NULL DEFAULT FALSE,
            text        TEXT NOT NULL,
            mentions    INTEGER[] NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            edited_at   TIMESTAMPTZ,
            deleted_at  TIMESTAMPTZ
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_comments_dt ON kpi_downtime_comments (downtime_id)")
    # Комментарии относятся к строке описания простоя (станция, причина, минуты), а не к простою целиком
    await pool.execute(
        "ALTER TABLE kpi_downtime_comments ADD COLUMN IF NOT EXISTS item_id INTEGER REFERENCES kpi_downtime_items(id) ON DELETE CASCADE"
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_comments_item ON kpi_downtime_comments (item_id)")
    # комментарии, оставленные раньше к простою целиком, переносим на его первую строку
    await pool.execute(
        "UPDATE kpi_downtime_comments c SET item_id = (SELECT min(i.id) FROM kpi_downtime_items i WHERE i.downtime_id = c.downtime_id) "
        "WHERE c.item_id IS NULL"
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_notifications (
            id          BIGSERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            downtime_id INTEGER NOT NULL REFERENCES kpi_downtimes(id) ON DELETE CASCADE,
            comment_id  INTEGER REFERENCES kpi_downtime_comments(id) ON DELETE CASCADE,
            from_name   TEXT NOT NULL,
            snippet     TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            read_at     TIMESTAMPTZ
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_kpi_notif_user ON kpi_notifications (user_id, read_at, created_at DESC)")


class CommentBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    parent_id: int | None = None
    is_oto: bool = False
    oto_request: bool = False
    mentions: list[int] = Field(default_factory=list, max_length=20)


class CommentEdit(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    mentions: list[int] = Field(default_factory=list, max_length=20)


def _can_oto(user: dict[str, Any]) -> bool:
    return user["role"] in OTO_ROLES or user.get("department") == "oto"


async def _active_ids(pool: asyncpg.Pool, ids: list[int], exclude: int) -> list[int]:
    if not ids:
        return []
    rows = await pool.fetch(
        "SELECT id FROM app_users WHERE id = ANY($1::int[]) AND is_active AND status = 'active' AND id <> $2", ids, exclude
    )
    return [r["id"] for r in rows]


async def _notify(conn: asyncpg.Connection, user_ids: list[int], kind: str, downtime_id: int, comment_id: int, from_name: str, text: str) -> None:
    snippet = " ".join(text.split())[:140]
    for uid in user_ids:
        await conn.execute(
            "INSERT INTO kpi_notifications (user_id, kind, downtime_id, comment_id, from_name, snippet) VALUES ($1,$2,$3,$4,$5,$6)",
            uid, kind, downtime_id, comment_id, from_name, snippet,
        )


@router.get("/mentionable")
async def mentionable(request: Request, _: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    """Активные пользователи для подсказки по @: имя, логин, отдел и должность."""
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, login, last_name, first_name, role, department FROM app_users WHERE is_active AND status = 'active' ORDER BY last_name, first_name"
    )
    return [
        {"id": r["id"], "login": r["login"], "name": f"{r['last_name']} {r['first_name']}", "role_name": ROLES.get(r["role"], r["role"]),
         "department": r["department"], "department_name": DEPARTMENTS.get(r["department"] or "", "")}
        for r in rows
    ]


@router.get("/items/{item_id}/comments")
async def list_comments(item_id: int, request: Request) -> list[dict[str, Any]]:
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, parent_id, user_id, user_name, department, is_oto, oto_request, text, mentions, created_at, edited_at, deleted_at "
        "FROM kpi_downtime_comments WHERE item_id = $1 ORDER BY created_at, id",
        item_id,
    )
    parents_with_children = {r["parent_id"] for r in rows if r["parent_id"] and not r["deleted_at"]}
    all_ids = sorted({m for r in rows if not r["deleted_at"] for m in r["mentions"]})
    names = {}
    if all_ids:
        names = {u["id"]: f"{u['last_name']} {u['first_name']}" for u in await pool.fetch(
            "SELECT id, last_name, first_name FROM app_users WHERE id = ANY($1::int[])", all_ids)}
    out = []
    for r in rows:
        if r["deleted_at"]:
            # удалённый комментарий остаётся заглушкой, только если на него есть ответы
            if r["id"] not in parents_with_children:
                continue
            out.append({"id": r["id"], "parent_id": r["parent_id"], "deleted": True})
            continue
        out.append({
            "id": r["id"], "parent_id": r["parent_id"], "user_id": r["user_id"], "user_name": r["user_name"],
            "department": r["department"], "department_name": DEPARTMENTS.get(r["department"] or "", ""),
            "is_oto": r["is_oto"], "oto_request": r["oto_request"], "text": r["text"], "mentions": list(r["mentions"]),
            "mention_names": [names[m] for m in r["mentions"] if m in names],
            "created_at": r["created_at"], "edited_at": r["edited_at"], "deleted": False,
        })
    return out


@router.post("/items/{item_id}/comments", status_code=201)
async def add_comment(
    item_id: int, body: CommentBody, request: Request, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, int]:
    pool: asyncpg.Pool = request.app.state.pool
    downtime_id = await pool.fetchval("SELECT downtime_id FROM kpi_downtime_items WHERE id = $1", item_id)
    if downtime_id is None:
        raise HTTPException(status_code=404, detail="Строка описания простоя не найдена: сначала сохраните её")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Введите текст комментария")
    from_oto = user.get("department") == "oto"   # метка «ОТО» ставится автоматически по категории автора, галочки нет
    parent_author: int | None = None
    if body.parent_id is not None:
        parent = await pool.fetchrow("SELECT user_id, item_id, deleted_at FROM kpi_downtime_comments WHERE id = $1", body.parent_id)
        if parent is None or parent["item_id"] != item_id or parent["deleted_at"]:
            raise HTTPException(status_code=404, detail="Комментарий, на который вы отвечаете, не найден")
        parent_author = parent["user_id"]
    mention_ids = await _active_ids(pool, list(dict.fromkeys(body.mentions)), user["id"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            cid = await conn.fetchval(
                "INSERT INTO kpi_downtime_comments (downtime_id, item_id, parent_id, user_id, user_name, department, is_oto, oto_request, text, mentions) "
                "VALUES ($1,$10,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
                downtime_id, body.parent_id, user["id"], user["full_name"], user.get("department"),
                from_oto, body.oto_request and not from_oto, text, mention_ids, item_id,
            )
            notified: set[int] = set()
            await _notify(conn, mention_ids, "mention", downtime_id, cid, user["full_name"], text)
            notified.update(mention_ids)
            if parent_author and parent_author != user["id"] and parent_author not in notified:
                await _notify(conn, [parent_author], "reply", downtime_id, cid, user["full_name"], text)
                notified.add(parent_author)
            if body.oto_request and not from_oto:
                oto = await conn.fetch("SELECT id FROM app_users WHERE department = 'oto' AND is_active AND status = 'active' AND id <> $1", user["id"])
                targets = [r["id"] for r in oto if r["id"] not in notified]
                await _notify(conn, targets, "oto", downtime_id, cid, user["full_name"], text)
    return {"id": cid}


@router.put("/comments/{comment_id}")
async def edit_comment(
    comment_id: int, body: CommentEdit, request: Request, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    c = await pool.fetchrow("SELECT id, downtime_id, user_id, mentions, deleted_at FROM kpi_downtime_comments WHERE id = $1", comment_id)
    if c is None or c["deleted_at"]:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    if c["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Изменять можно только свои комментарии")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Введите текст комментария")
    mention_ids = await _active_ids(pool, list(dict.fromkeys(body.mentions)), user["id"])
    new_ids = [m for m in mention_ids if m not in set(c["mentions"])]   # уведомляем только тех, кого добавили при правке
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE kpi_downtime_comments SET text = $2, mentions = $3, edited_at = now() WHERE id = $1", comment_id, text, mention_ids
            )
            await _notify(conn, new_ids, "mention", c["downtime_id"], comment_id, user["full_name"], text)
    return {"status": "ok"}


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: int, request: Request, user: dict[str, Any] = Depends(require_editor)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    c = await pool.fetchrow("SELECT user_id, deleted_at FROM kpi_downtime_comments WHERE id = $1", comment_id)
    if c is None or c["deleted_at"]:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    if c["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Удалить комментарий может только его автор или администратор")
    await pool.execute("UPDATE kpi_downtime_comments SET deleted_at = now() WHERE id = $1", comment_id)
    return {"status": "ok"}


# ------------------------------------------------------------------ уведомления
@router.get("/notifications")
async def notifications(request: Request, limit: int = Query(default=30, ge=1, le=100), user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    unread = await pool.fetchval("SELECT count(*) FROM kpi_notifications WHERE user_id = $1 AND read_at IS NULL", user["id"])
    rows = await pool.fetch(
        "SELECT n.id, n.kind, n.downtime_id, c.item_id, n.from_name, n.snippet, n.created_at, n.read_at, d.screen_id, d.idx, d.prod_date "
        "FROM kpi_notifications n JOIN kpi_downtimes d ON d.id = n.downtime_id "
        "LEFT JOIN kpi_downtime_comments c ON c.id = n.comment_id "
        "WHERE n.user_id = $1 ORDER BY n.created_at DESC, n.id DESC LIMIT $2",
        user["id"], limit,
    )
    return {"unread": unread, "items": [dict(r) for r in rows]}


class ReadBody(BaseModel):
    ids: list[int] | None = None   # None = отметить все


@router.post("/notifications/read")
async def notifications_read(body: ReadBody, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    if body.ids is None:
        await pool.execute("UPDATE kpi_notifications SET read_at = now() WHERE user_id = $1 AND read_at IS NULL", user["id"])
    else:
        await pool.execute(
            "UPDATE kpi_notifications SET read_at = now() WHERE user_id = $1 AND read_at IS NULL AND id = ANY($2::bigint[])", user["id"], body.ids
        )
    return {"status": "ok"}
