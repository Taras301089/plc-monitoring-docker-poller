"""История связи с ПЛК: поллер пишет событие (plc_link_events) только при смене состояния связи, а не при каждом опросе."""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent / "python-async-poller"))

from database import Database  # noqa: E402


def _db(pool):
    db = object.__new__(Database)      # без настроек и своего подключения: используем пул тестовой базы
    db._pool = pool
    return db


_n = 0


async def _plc(pool):
    global _n
    _n += 1
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://10.0.0.1:4840') RETURNING id", f"t{_n}")


async def _events(pool, plc_id):
    return [(r["state"], r["reason"]) for r in await pool.fetch(
        "SELECT state, reason FROM plc_link_events WHERE plc_id = $1 ORDER BY id", plc_id)]


async def test_event_only_when_state_changes(app, pool):
    plc = await _plc(pool)
    db = _db(pool)
    await db.update_connection_status(plc, True)
    await db.update_connection_status(plc, True)
    await db.update_connection_status(plc, True)
    assert await _events(pool, plc) == [("ok", "")]
    await db.update_connection_status(plc, False, "BadUserAccessDenied")
    await db.update_connection_status(plc, False, "BadUserAccessDenied")
    assert await _events(pool, plc) == [("ok", ""), ("no_link", "BadUserAccessDenied")]
    await db.update_connection_status(plc, True)
    assert await _events(pool, plc) == [("ok", ""), ("no_link", "BadUserAccessDenied"), ("ok", "")]


async def test_each_plc_has_its_own_history(app, pool):
    a, b = await _plc(pool), await _plc(pool)
    db = _db(pool)
    await db.update_connection_status(a, True)
    await db.update_connection_status(b, False, "x")
    assert await _events(pool, a) == [("ok", "")]
    assert await _events(pool, b) == [("no_link", "x")]


async def test_status_row_still_updated(app, pool):
    plc = await _plc(pool)
    await _db(pool).update_connection_status(plc, False, "err")
    row = await pool.fetchrow("SELECT is_connected, last_error FROM plc_connection_status WHERE plc_id = $1", plc)
    assert row["is_connected"] is False and row["last_error"] == "err"


async def test_missing_events_table_does_not_break_polling(app, pool):
    """Таблицу создаёт API при запуске; если её ещё нет, статус связи всё равно сохраняется."""
    import trends
    plc = await _plc(pool)
    await pool.execute("DROP TABLE plc_link_events")
    try:
        await _db(pool).update_connection_status(plc, True)
        assert await pool.fetchval("SELECT is_connected FROM plc_connection_status WHERE plc_id = $1", plc) is True
    finally:
        await trends.ensure_trends_schema(pool)


async def test_long_reason_is_truncated(app, pool):
    plc = await _plc(pool)
    await _db(pool).update_connection_status(plc, False, "x" * 5000)
    assert len((await _events(pool, plc))[0][1]) <= 500
