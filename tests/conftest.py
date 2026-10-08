"""Общая настройка тестов.

Защита: тесты работают только с базой, имя которой заканчивается на _test.
Приложение запускается в тестовом процессе (настоящий код API, его старт создаёт таблицы).
"""
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://n8n_user:my_strong_password@localhost:5432/general_data_hub_test",
)
TEST_PASSWORD = "Test-Pass-12345"
# Тестовые пользователи: логин -> (роль, подразделение)
TEST_USERS = {
    "t_admin": ("admin", None),
    "t_chief": ("chief", "production"),
    "t_area": ("area_head", "production"),
    "t_master": ("master", "production"),
    "t_oto": ("maintenance", "oto"),
    "t_viewer": ("viewer", None),
}
# Таблицы, которые между тестами НЕ очищаются (пользователи, сессии, справочники и экраны со стартовыми данными)
KEEP_TABLES = {"app_users", "app_sessions"}


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def pytest_configure(config):
    name = _db_name(TEST_DATABASE_URL)
    if not name.endswith("_test"):
        pytest.exit(f"ОТКАЗ: база '{name}' не оканчивается на _test. Тесты не запускаются.", returncode=2)
    # Приложение читает адрес базы и папку отчётов при импорте, поэтому задаём до импорта
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["REPORTS_DIR"] = tempfile.mkdtemp(prefix="plc_test_reports_")
    sys.path.insert(0, str(ROOT / "fastapi-api"))


@pytest.fixture(scope="session")
async def pool():
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture(scope="session")
async def app(pool):
    """Приложение на чистой тестовой базе: схему и стартовые данные создаёт его собственный старт."""
    assert _db_name(TEST_DATABASE_URL).endswith("_test")
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    # Базовые таблицы создаёт init.sql (без команд psql: создание баз и \connect)
    lines = (ROOT / "init.sql").read_text(encoding="utf-8").splitlines()
    sql = "\n".join(l for l in lines if not l.startswith("\\") and not l.upper().startswith("CREATE DATABASE"))
    await pool.execute(sql)
    import main

    async with main.app.router.lifespan_context(main.app):
        yield main.app


@pytest.fixture(scope="session")
async def users(app, pool):
    """Тестовые пользователи по ролям; возвращает {логин: клиент с cookie входа}."""
    from auth import hash_password

    for login, (role, dept) in TEST_USERS.items():
        await pool.execute(
            "INSERT INTO app_users (login, last_name, first_name, role, password_hash, must_change_password, department) "
            "VALUES ($1, $2, $3, $4, $5, FALSE, $6)",
            login, login.upper(), "Тест", role, hash_password(TEST_PASSWORD), dept,
        )
    clients = {}
    for login in TEST_USERS:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        r = await c.post("/api/auth/login", json={"login": login, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"вход {login}: {r.status_code} {r.text}"
        clients[login] = c
    yield clients
    for c in clients.values():
        await c.aclose()


@pytest.fixture
async def anon(app):
    """Клиент без входа."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def clean_data(request, pool):
    """Перед каждым тестом очищает данные, созданные тестами; пользователи и сессии остаются.

    Срабатывает только если тест использует приложение (app/users/anon), чтобы тесты без базы не ждали."""
    if not ({"app", "users", "anon"} & set(request.fixturenames)):
        return
    assert _db_name(TEST_DATABASE_URL).endswith("_test")
    tables = [
        r["tablename"]
        for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if r["tablename"] not in KEEP_TABLES and r["tablename"] not in SEEDED_TABLES
    ]
    if tables:
        await pool.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")


SEEDED_TABLES: set[str] = set()
