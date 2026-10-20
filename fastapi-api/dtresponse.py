"""Ответ отдела ОТО / ИТО на ответственность за отставание (шаг 2 задачи T-15).

Строка описания простоя с ответственным ОТО или ИТО ждёт подтверждения отдела. Отдел отвечает: «Согласен» (все минуты строки у отдела),
«Частично» (отдел принимает k минут, остаток к ПР), «Не согласен» (комментарий обязателен, минуты остаются у отдела, статус «спорно»).
Отвечают сотрудники своего подразделения (роль не «Просмотр»), начальник цеха и администратор за любой отдел. Срока нет: ответ можно
дать и изменить в любой день; отозвать могут автор ответа и администратор. Каждое изменение пишется в журнал kpi_downtime_responses_log.
Таблицы создаёт downtime.ensure_downtime_schema; статус и деление минут считают downtime.owner_status и responsibility.owner_split."""
from __future__ import annotations

from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import require_user
from downtime import ANSWERING, OWNERS, _author

router = APIRouter(prefix="/api")

ANY_DEPARTMENT = {"admin", "chief"}   # отвечают за любой отдел


class ResponseBody(BaseModel):
    answer: Literal["agree", "partial", "disagree"]
    minutes: float | None = None            # только для «Частично»: сколько минут принимает отдел
    comment: str = Field(default="", max_length=500)


def can_respond(user: dict[str, Any] | None, department: str | None) -> bool:
    """Право ответить за отдел: вошедший не «Просмотр»; администратор и начальник цеха за любой отдел, остальные только за своё подразделение."""
    if not user or user.get("role") == "viewer" or department not in ANSWERING:
        return False
    return user.get("role") in ANY_DEPARTMENT or user.get("department") == department


def _check_rights(user: dict[str, Any], department: str) -> None:
    if user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="У роли «Просмотр» нет права отвечать за отдел")
    if not can_respond(user, department):
        name = OWNERS[department]
        raise HTTPException(status_code=403, detail=f"Ответить за {name} может только сотрудник подразделения {name}, начальник цеха или администратор")


async def _item(pool: asyncpg.Pool, item_id: int) -> asyncpg.Record:
    it = await pool.fetchrow("SELECT id, owner, minutes FROM kpi_downtime_items WHERE id = $1", item_id)
    if it is None:
        raise HTTPException(status_code=404, detail="Строка описания простоя не найдена")
    return it


def _log_sql() -> str:
    return ("INSERT INTO kpi_downtime_responses_log (item_id, department, action, old_answer, old_minutes, old_comment, "
            "new_answer, new_minutes, new_comment, changed_by, changed_by_name) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)")


@router.put("/downtime-items/{item_id}/response")
async def put_response(item_id: int, body: ResponseBody, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    it = await _item(pool, item_id)
    dept = it["owner"]
    if dept not in ANSWERING:
        raise HTTPException(status_code=422, detail="Ответ отдела нужен только для ответственных ОТО и ИТО: у ПР и ДР строка сразу подтверждена")
    _check_rights(user, dept)
    total = float(it["minutes"])
    comment = body.comment.strip()
    minutes: float | None = None
    if body.answer == "partial":
        if body.minutes is None or not (0 < body.minutes < total):
            raise HTTPException(status_code=422, detail=f"Частично: укажите, сколько минут принимает отдел, больше 0 и меньше {total:g} мин")
        minutes = round(body.minutes, 1)
        if not (0 < minutes < total):
            raise HTTPException(status_code=422, detail=f"Частично: укажите, сколько минут принимает отдел, больше 0 и меньше {total:g} мин")
    elif body.answer == "disagree" and not comment:
        raise HTTPException(status_code=422, detail="Не согласен: напишите комментарий, почему отдел не согласен")
    uname = user["full_name"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT department, answer, minutes, comment FROM kpi_downtime_responses WHERE item_id = $1 FOR UPDATE", item_id)
            row = await conn.fetchrow(
                "INSERT INTO kpi_downtime_responses (item_id, department, answer, minutes, comment, user_id, user_name) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (item_id) DO UPDATE SET department = EXCLUDED.department, answer = EXCLUDED.answer, "
                "minutes = EXCLUDED.minutes, comment = EXCLUDED.comment, user_id = EXCLUDED.user_id, user_name = EXCLUDED.user_name, updated_at = now() "
                "RETURNING updated_at",
                item_id, dept, body.answer, minutes, comment, user["id"], uname)
            # прежний ответ другого отдела (ответственного сменили) в журнале считается «было»
            await conn.execute(_log_sql(), item_id, dept, "update" if old else "create",
                               old["answer"] if old else None, old["minutes"] if old else None, old["comment"] if old else None,
                               body.answer, minutes, comment, user["id"], uname)
    return {"status": "ok", "response": {"answer": body.answer, "minutes": minutes, "comment": comment, "user_id": user["id"], "user_name": uname,
                                         "author": _author(user.get("last_name"), user.get("role"), uname), "updated_at": row["updated_at"]}}


@router.delete("/downtime-items/{item_id}/response")
async def delete_response(item_id: int, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Отозвать ответ: строка снова «ждёт подтверждения». Отзывают только автор ответа и администратор; прежний ответ остаётся в журнале."""
    pool: asyncpg.Pool = request.app.state.pool
    await _item(pool, item_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT department, answer, minutes, comment, user_id, user_name FROM kpi_downtime_responses WHERE item_id = $1 FOR UPDATE", item_id)
            if old is None:
                raise HTTPException(status_code=404, detail="Ответа отдела на эту строку нет")
            if not (user["role"] == "admin" or old["user_id"] == user["id"]):
                raise HTTPException(status_code=403, detail=f"Ответ дал(а) {old['user_name']}: отозвать его могут только автор ответа и администратор")
            await conn.execute(_log_sql(), item_id, old["department"], "delete", old["answer"], old["minutes"], old["comment"],
                               None, None, None, user["id"], user["full_name"])
            await conn.execute("DELETE FROM kpi_downtime_responses WHERE item_id = $1", item_id)
    return {"status": "ok"}
