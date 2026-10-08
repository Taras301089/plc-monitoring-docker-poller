"""Проверка самой тестовой подготовки: пользователи, вход, очистка данных между тестами."""
import pytest

from conftest import TEST_USERS


async def test_every_role_can_login(users):
    for login, (role, _dept) in TEST_USERS.items():
        r = await users[login].get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["user"]["role"] == role


async def test_anonymous_is_not_authenticated(anon):
    r = await anon.get("/api/auth/me")
    assert r.json()["authenticated"] is False


async def test_data_cleanup_part1_writes(users, pool):
    await pool.execute("INSERT INTO plcs (name, opc_endpoint) VALUES ('tmp', 'opc.tcp://x')")
    assert await pool.fetchval("SELECT count(*) FROM plcs") == 1


async def test_data_cleanup_part2_is_clean(users, pool):
    assert await pool.fetchval("SELECT count(*) FROM plcs") == 0
    assert await pool.fetchval("SELECT count(*) FROM app_users") == len(TEST_USERS)
