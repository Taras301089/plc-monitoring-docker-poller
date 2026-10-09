"""Среда APP_ENV: по умолчанию dev (запись в ПЛК запрещена); prod включается явно (на сервере). Метка среды на сайте."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "python-async-poller"))

from models import Plc  # noqa: E402
from opc_client import PlcOpcClient  # noqa: E402

HTML = (ROOT / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
REF = (ROOT / "fastapi-api" / "static" / "reference.html").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


async def _fl_screen(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('flplc', 'opc.tcp://10.0.0.1:4840') RETURNING id")
    return await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('FL T', $1, 'fl_counter', "
        "'{\"plan_day\": \"ns=3;s=PlanDay\"}'::jsonb) RETURNING id", plc)


async def test_env_endpoint_default_is_dev(app, anon, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    assert (await anon.get("/api/env")).json() == {"env": "dev"}


async def test_env_endpoint_prod_only_when_explicit(app, anon, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    assert (await anon.get("/api/env")).json() == {"env": "prod"}
    monkeypatch.setenv("APP_ENV", "production-ish")
    assert (await anon.get("/api/env")).json() == {"env": "dev"}      # любое значение, кроме prod, считается разработкой


async def test_plan_day_write_is_blocked_in_dev_and_journaled(users, pool, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    scr = await _fl_screen(pool)
    r = await users["t_master"].post(f"/api/kpi/screens/{scr}/plan-day", json={"value": 80})
    assert r.status_code == 403 and "Режим разработки" in r.json()["detail"]
    row = await pool.fetchrow("SELECT ok, error, new_value FROM kpi_plan_writes WHERE screen_id = $1", scr)
    assert row["ok"] is False and "Режим разработки" in row["error"] and row["new_value"] == 80


async def test_plan_day_passes_the_guard_in_prod(users, pool, monkeypatch):
    """В prod запрос доходит до поллера (его адрес в тесте недоступен, поэтому 502, а не 403)."""
    import main
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setattr(main, "POLLER_URL", "http://127.0.0.1:9")
    scr = await _fl_screen(pool)
    r = await users["t_master"].post(f"/api/kpi/screens/{scr}/plan-day", json={"value": 80})
    assert r.status_code == 502 and "Поллер недоступен" in r.json()["detail"]


async def test_poller_write_int_is_blocked_in_dev(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    client = PlcOpcClient(Plc(id=1, name="T", opc_endpoint="opc.tcp://10.0.0.1:4840", poll_interval_ms=1000))
    called = []

    async def connected():
        called.append(1)
        return True

    client._ensure_connected_unlocked = connected
    with pytest.raises(PermissionError, match="Режим разработки"):
        await client.write_int("ns=3;s=PlanDay", 80)
    assert called == []                     # до подключения к ПЛК дело не дошло


async def test_poller_write_int_reaches_plc_layer_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    client = PlcOpcClient(Plc(id=1, name="T", opc_endpoint="opc.tcp://10.0.0.1:4840", poll_interval_ms=1000))

    async def not_connected():
        return False

    client._ensure_connected_unlocked = not_connected
    with pytest.raises(ConnectionError):
        await client.write_int("ns=3;s=PlanDay", 80)


def test_compose_passes_env_with_safe_default_to_api_and_poller():
    assert COMPOSE.count("APP_ENV: ${APP_ENV:-dev}") == 2


def test_site_shows_environment_badge_only_outside_prod():
    assert 'id="env-badge"' in HTML and "Среда: разработка" in HTML
    assert "/api/env" in HTML and "env !== 'prod'" in HTML
    i = HTML.index('id="env-badge"')
    assert "title=" in HTML[i - 200: i + 400]


def test_reference_explains_prod_env_file_for_server():
    assert "APP_ENV=prod" in REF
