"""Пользователи и вход: свои учётные записи, пароли в виде scrypt-хэша, сессии по cookie."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

ROLES: dict[str, str] = {
    "admin": "Администратор",
    "chief": "Начальник цеха",
    "area_head": "Начальник участка",
    "master": "Мастер",
    "maintenance": "ТО",
    "viewer": "Просмотр",
}
DEPARTMENTS: dict[str, str] = {
    "production": "Производство",
    "oto": "ОТО",
    "ito": "ИТО",
}
COOKIE_NAME = "plcmon_session"
SESSION_DAYS = 30
_LOGIN_RE = re.compile(r"^[a-z0-9._-]{3,40}$")

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ схема
async def ensure_auth_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS app_users (
            id                   SERIAL PRIMARY KEY,
            login                TEXT    NOT NULL UNIQUE,
            last_name            TEXT    NOT NULL,
            first_name           TEXT    NOT NULL,
            role                 TEXT    NOT NULL,
            password_hash        TEXT    NOT NULL,
            must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
            is_active            BOOLEAN NOT NULL DEFAULT TRUE,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login_at        TIMESTAMPTZ
        )
        """
    )
    await pool.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
    await pool.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS department TEXT")
    await pool.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_view JSONB")
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS app_sessions (
            token_hash   TEXT PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_app_sessions_user ON app_sessions (user_id)")
    # Журнал входов для статистики (кто и сколько раз заходил)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS app_login_events (
            id      BIGSERIAL PRIMARY KEY,
            ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
            user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_app_login_events_user_ts ON app_login_events (user_id, ts)")


# ------------------------------------------------------------------ пароли и токены
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$14$8$1${}${}".format(base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n_exp, r, p, salt_b64, hash_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=2 ** int(n_exp), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": d["id"],
        "login": d["login"],
        "last_name": d["last_name"],
        "first_name": d["first_name"],
        "full_name": f"{d['last_name']} {d['first_name']}",
        "role": d["role"],
        "role_name": ROLES.get(d["role"], d["role"]),
        "department": d.get("department"),
        "department_name": DEPARTMENTS.get(d.get("department") or "", ""),
        "must_change_password": d["must_change_password"],
    }


async def _start_session(pool: asyncpg.Pool, response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    await pool.execute(
        "INSERT INTO app_sessions (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        _token_hash(token),
        user_id,
        _now() + timedelta(days=SESSION_DAYS),
    )
    await pool.execute("INSERT INTO app_login_events (user_id) VALUES ($1)", user_id)
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/"
    )


# ------------------------------------------------------------------ зависимости
async def current_user(request: Request, response: Response) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    pool: asyncpg.Pool = request.app.state.pool
    row = await pool.fetchrow(
        """
        SELECT u.id, u.login, u.last_name, u.first_name, u.role, u.department, u.must_change_password,
               u.is_active, u.status, s.expires_at, s.last_seen_at
        FROM app_sessions s JOIN app_users u ON u.id = s.user_id
        WHERE s.token_hash = $1
        """,
        _token_hash(token),
    )
    now = _now()
    if row is None or not row["is_active"] or row["status"] != "active" or row["expires_at"] < now:
        return None
    if now - row["last_seen_at"] > timedelta(hours=1):
        await pool.execute(
            "UPDATE app_sessions SET last_seen_at = $2, expires_at = $3 WHERE token_hash = $1",
            _token_hash(token),
            now,
            now + timedelta(days=SESSION_DAYS),
        )
        response.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/"
        )
    return _public(row)


async def require_user(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход в систему")
    return user


async def require_editor(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Вошедший пользователь, у которого есть право менять настройки (все роли, кроме «Просмотр»)."""
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="У вашей роли нет права менять настройки")
    return user


async def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


# ------------------------------------------------------------------ модели запросов
class SetupBody(BaseModel):
    login: str
    last_name: str = Field(min_length=1, max_length=60)
    first_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=128)


class RegisterBody(SetupBody):
    department: str | None = None


class LoginBody(BaseModel):
    login: str
    password: str = Field(max_length=128)


class ChangePasswordBody(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserCreateBody(BaseModel):
    login: str
    last_name: str = Field(min_length=1, max_length=60)
    first_name: str = Field(min_length=1, max_length=60)
    role: str
    password: str = Field(min_length=8, max_length=128)
    department: str | None = None


class UserUpdateBody(BaseModel):
    last_name: str = Field(min_length=1, max_length=60)
    first_name: str = Field(min_length=1, max_length=60)
    role: str
    is_active: bool = True
    department: str | None = None


class ResetPasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=128)


def _norm_login(login: str) -> str:
    value = login.strip().lower()
    if not _LOGIN_RE.match(value):
        raise HTTPException(status_code=422, detail="Логин: 3–40 символов, латиница, цифры, точка, дефис, подчёркивание")
    return value


def _check_department(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if value not in DEPARTMENTS:
        raise HTTPException(status_code=422, detail="Неизвестный отдел")
    return value


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(status_code=422, detail="Неизвестная роль")
    return role


# ------------------------------------------------------------------ вход и выход
@router.get("/auth/me")
async def auth_me(request: Request, user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    count = await pool.fetchval("SELECT count(*) FROM app_users")
    pending = 0
    last_view = None
    if user is not None:
        raw = await pool.fetchval("SELECT last_view FROM app_users WHERE id = $1", user["id"])
        last_view = json.loads(raw) if isinstance(raw, str) else raw
        if user["role"] == "admin":
            pending = await pool.fetchval("SELECT count(*) FROM app_users WHERE status = 'pending'")
    return {
        "authenticated": user is not None,
        "user": user,
        "last_view": last_view,
        "setup_required": count == 0,
        "roles": ROLES,
        "departments": DEPARTMENTS,
        "pending_count": pending,
    }


class LastViewBody(BaseModel):
    tab: str = Field(pattern="^[a-z]{2,20}$")
    screen: int | None = Field(default=None, ge=1)


@router.put("/auth/last-view")
async def auth_last_view(
    body: LastViewBody, request: Request, user: dict[str, Any] = Depends(require_user)
) -> dict[str, str]:
    """Запоминает последний открытый экран пользователя (вкладка и экран Andon), чтобы открыть его при следующем входе."""
    await request.app.state.pool.execute(
        "UPDATE app_users SET last_view = $2::jsonb WHERE id = $1",
        user["id"],
        json.dumps({"tab": body.tab, "screen": body.screen}),
    )
    return {"status": "ok"}


@router.post("/auth/register", status_code=201)
async def auth_register(body: RegisterBody, request: Request) -> dict[str, str]:
    """Заявка на регистрацию: учётная запись создаётся «ожидающей», войти можно после подтверждения администратором."""
    pool: asyncpg.Pool = request.app.state.pool
    login = _norm_login(body.login)
    department = _check_department(body.department)
    if await pool.fetchval("SELECT count(*) FROM app_users") == 0:
        raise HTTPException(status_code=409, detail="Сначала нужно создать администратора")
    try:
        await pool.execute(
            "INSERT INTO app_users (login, last_name, first_name, role, password_hash, must_change_password, status, department) "
            "VALUES ($1, $2, $3, 'viewer', $4, FALSE, 'pending', $5)",
            login,
            body.last_name.strip(),
            body.first_name.strip(),
            hash_password(body.password),
            department,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже есть") from exc
    return {"status": "pending"}


@router.post("/auth/setup")
async def auth_setup(body: SetupBody, request: Request, response: Response) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    login = _norm_login(body.login)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("LOCK TABLE app_users IN EXCLUSIVE MODE")
            if await conn.fetchval("SELECT count(*) FROM app_users") > 0:
                raise HTTPException(status_code=409, detail="Администратор уже создан")
            user_id = await conn.fetchval(
                "INSERT INTO app_users (login, last_name, first_name, role, password_hash, must_change_password) "
                "VALUES ($1, $2, $3, 'admin', $4, FALSE) RETURNING id",
                login,
                body.last_name.strip(),
                body.first_name.strip(),
                hash_password(body.password),
            )
    await _start_session(pool, response, user_id)
    row = await pool.fetchrow("SELECT * FROM app_users WHERE id = $1", user_id)
    return {"user": _public(row)}


@router.post("/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    login = body.login.strip().lower()
    row = await pool.fetchrow("SELECT * FROM app_users WHERE login = $1", login)
    ok = row is not None and row["is_active"] and verify_password(body.password, row["password_hash"])
    if not ok:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if row["status"] == "pending":
        raise HTTPException(status_code=403, detail="Ваша заявка ещё не подтверждена администратором")
    await pool.execute("UPDATE app_users SET last_login_at = now() WHERE id = $1", row["id"])
    await _start_session(pool, response, row["id"])
    return {"user": _public(row)}


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await request.app.state.pool.execute("DELETE FROM app_sessions WHERE token_hash = $1", _token_hash(token))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.post("/auth/change-password")
async def auth_change_password(
    body: ChangePasswordBody, request: Request, user: dict[str, Any] = Depends(require_user)
) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    row = await pool.fetchrow("SELECT password_hash FROM app_users WHERE id = $1", user["id"])
    if row is None or not verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="Новый пароль должен отличаться от текущего")
    await pool.execute(
        "UPDATE app_users SET password_hash = $2, must_change_password = FALSE WHERE id = $1",
        user["id"],
        hash_password(body.new_password),
    )
    return {"status": "ok"}


# ------------------------------------------------------------------ управление пользователями (администратор)
@router.get("/users")
async def users_list(request: Request, _: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, login, last_name, first_name, role, department, must_change_password, is_active, status, "
        "created_at, last_login_at FROM app_users ORDER BY last_name, first_name"
    )
    out = []
    for r in rows:
        d = _public(r)
        d["is_active"] = r["is_active"]
        d["status"] = r["status"]
        d["created_at"] = r["created_at"].isoformat()
        d["last_login_at"] = r["last_login_at"].isoformat() if r["last_login_at"] else None
        out.append(d)
    return out


@router.post("/users", status_code=201)
async def users_create(
    body: UserCreateBody, request: Request, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    login, role = _norm_login(body.login), _check_role(body.role)
    department = _check_department(body.department)
    try:
        user_id = await pool.fetchval(
            "INSERT INTO app_users (login, last_name, first_name, role, password_hash, must_change_password, department) "
            "VALUES ($1, $2, $3, $4, $5, TRUE, $6) RETURNING id",
            login,
            body.last_name.strip(),
            body.first_name.strip(),
            role,
            hash_password(body.password),
            department,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже есть") from exc
    return {"id": user_id}


async def _other_active_admins(pool: asyncpg.Pool, user_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM app_users WHERE role = 'admin' AND is_active AND id <> $1", user_id
    )


@router.put("/users/{user_id}")
async def users_update(
    user_id: int, body: UserUpdateBody, request: Request, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    role = _check_role(body.role)
    department = _check_department(body.department)
    target = await pool.fetchrow("SELECT role, is_active FROM app_users WHERE id = $1", user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target["role"] == "admin" and target["is_active"] and (role != "admin" or not body.is_active):
        if await _other_active_admins(pool, user_id) == 0:
            raise HTTPException(status_code=400, detail="Нельзя убрать единственного активного администратора")
    await pool.execute(
        "UPDATE app_users SET last_name = $2, first_name = $3, role = $4, is_active = $5, department = $6 WHERE id = $1",
        user_id,
        body.last_name.strip(),
        body.first_name.strip(),
        role,
        body.is_active,
        department,
    )
    if not body.is_active:
        await pool.execute("DELETE FROM app_sessions WHERE user_id = $1", user_id)
    return {"status": "ok"}


class ApproveBody(BaseModel):
    role: str


@router.post("/users/{user_id}/approve")
async def users_approve(
    user_id: int, body: ApproveBody, request: Request, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, str]:
    role = _check_role(body.role)
    result = await request.app.state.pool.execute(
        "UPDATE app_users SET status = 'active', role = $2 WHERE id = $1 AND status = 'pending'", user_id, role
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {"status": "ok"}


@router.post("/users/{user_id}/reject")
async def users_reject(
    user_id: int, request: Request, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, str]:
    result = await request.app.state.pool.execute(
        "DELETE FROM app_users WHERE id = $1 AND status = 'pending'", user_id
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {"status": "ok"}


@router.post("/users/{user_id}/reset-password")
async def users_reset_password(
    user_id: int, body: ResetPasswordBody, request: Request, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    result = await pool.execute(
        "UPDATE app_users SET password_hash = $2, must_change_password = TRUE WHERE id = $1",
        user_id,
        hash_password(body.password),
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    await pool.execute("DELETE FROM app_sessions WHERE user_id = $1", user_id)
    return {"status": "ok"}
