"""Сбор аварий: биты слов массива MSG.W (UInt16) превращаются в события alarm_events (появление и снятие), запись только при смене."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent / "python-async-poller"))

from alarm_collector import AlarmCollector, apply_changes, diff_active, words_to_bits  # noqa: E402

T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
NODE = 'ns=3;s="MSG"."W"'


# ---------- чистая логика ----------
def test_words_to_bits_maps_word_and_bit_to_global_index():
    assert words_to_bits([0b1, 1 << 14], 2) == {0, 30}
    assert words_to_bits([0, 1], 2) == {16}
    assert words_to_bits([0xFFFF], 1) == set(range(16))


def test_words_beyond_word_count_are_ignored_and_values_are_masked_to_16_bits():
    assert words_to_bits([1, 1, 1], 2) == {0, 16}
    assert words_to_bits([0x10001], 1) == {0}


def test_non_integer_items_are_treated_as_zero():
    assert words_to_bits([None, 1, "x"], 3) == {16}


def test_diff_active():
    assert diff_active({1, 2}, {2, 3}) == ([3], [1])
    assert diff_active(set(), set()) == ([], [])


# ---------- запись событий ----------
async def _plc(pool, name="al1"):
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://10.0.0.1:4840') RETURNING id", name)


async def _rows(pool, plc):
    return [dict(r) for r in await pool.fetch(
        "SELECT bit_index, started_at, ended_at, active_at_start FROM alarm_events WHERE plc_id = $1 ORDER BY id", plc)]


async def test_open_and_close(app, pool):
    plc = await _plc(pool)
    await apply_changes(pool, plc, [5], [], T0, first=False)
    rows = await _rows(pool, plc)
    assert len(rows) == 1 and rows[0]["bit_index"] == 5 and rows[0]["ended_at"] is None and rows[0]["active_at_start"] is False
    await apply_changes(pool, plc, [], [5], T0 + timedelta(seconds=30), first=False)
    rows = await _rows(pool, plc)
    assert rows[0]["ended_at"] == T0 + timedelta(seconds=30)


async def test_active_at_start_flag_and_repeat_is_idempotent(app, pool):
    plc = await _plc(pool, "al2")
    await apply_changes(pool, plc, [7], [], T0, first=True)
    await apply_changes(pool, plc, [7], [], T0, first=True)          # повторная запись не создаёт дубль открытой аварии
    rows = await _rows(pool, plc)
    assert len(rows) == 1 and rows[0]["active_at_start"] is True


async def test_reappearance_creates_new_row(app, pool):
    plc = await _plc(pool, "al3")
    await apply_changes(pool, plc, [3], [], T0, first=False)
    await apply_changes(pool, plc, [], [3], T0 + timedelta(seconds=5), first=False)
    await apply_changes(pool, plc, [3], [], T0 + timedelta(seconds=10), first=False)
    assert len(await _rows(pool, plc)) == 2


# ---------- сборщик целиком (подставной клиент) ----------
class FakeClient:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    async def read_words(self, node_id, count):
        self.calls += 1
        f = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(f, Exception):
            raise f
        return f


def _collector(pool, clients):
    return AlarmCollector(lambda: pool, lambda: clients, stop=None)


async def _source(pool, plc, enabled=True, count=2):
    await pool.execute("INSERT INTO alarm_sources (plc_id, node_id, word_count, enabled) VALUES ($1, $2, $3, $4) "
                       "ON CONFLICT (plc_id) DO UPDATE SET enabled = $4", plc, NODE, count, enabled)


async def test_collector_lifecycle(app, pool):
    plc = await _plc(pool, "al4")
    await _source(pool, plc)
    client = FakeClient([[1, 0], [1, 0], [0, 0], [0, 1 << 2]])
    col = _collector(pool, {plc: client})
    await col.cycle_once(T0)                                           # бит 0 уже активен при первом чтении
    await col.cycle_once(T0 + timedelta(seconds=1))                    # без изменений: ничего не пишется
    rows = await _rows(pool, plc)
    assert len(rows) == 1 and rows[0]["active_at_start"] is True and rows[0]["ended_at"] is None
    await col.cycle_once(T0 + timedelta(seconds=2))                    # бит 0 снят
    await col.cycle_once(T0 + timedelta(seconds=3))                    # появился бит 18 (слово 1, бит 2)
    rows = await _rows(pool, plc)
    assert [r["bit_index"] for r in rows] == [0, 18]
    assert rows[0]["ended_at"] == T0 + timedelta(seconds=2)
    assert rows[1]["active_at_start"] is False and rows[1]["ended_at"] is None


async def test_restart_keeps_still_active_and_closes_cleared(app, pool):
    plc = await _plc(pool, "al5")
    await _source(pool, plc)
    await _collector(pool, {plc: FakeClient([[0b11, 0]])}).cycle_once(T0)       # биты 0 и 1 активны
    # «перезапуск поллера»: новый сборщик; бит 0 всё ещё активен, бит 1 снят, пока поллер не работал
    await _collector(pool, {plc: FakeClient([[0b01, 0]])}).cycle_once(T0 + timedelta(minutes=5))
    rows = {r["bit_index"]: r for r in await _rows(pool, plc)}
    assert rows[0]["ended_at"] is None and len([1 for r in await _rows(pool, plc) if r["bit_index"] == 0]) == 1
    # поллер не работал 5 минут: время снятия неизвестно, берётся последнее удачное чтение (T0), с признаком
    assert rows[1]["ended_at"] == T0
    assert await pool.fetchval("SELECT closed_in_gap FROM alarm_events WHERE plc_id = $1 AND bit_index = 1", plc) is True


async def test_read_failure_changes_nothing(app, pool):
    plc = await _plc(pool, "al6")
    await _source(pool, plc)
    col = _collector(pool, {plc: FakeClient([[1, 0], ConnectionError("нет связи")])})
    await col.cycle_once(T0)
    await col.cycle_once(T0 + timedelta(seconds=1))                    # исключение не пробрасывается
    rows = await _rows(pool, plc)
    assert len(rows) == 1 and rows[0]["ended_at"] is None


async def test_disabled_source_and_missing_client_are_skipped(app, pool):
    off = await _plc(pool, "al7")
    await _source(pool, off, enabled=False)
    noclient = await _plc(pool, "al8")
    await _source(pool, noclient)
    c = FakeClient([[1, 0]])
    await _collector(pool, {off: c}).cycle_once(T0)
    assert c.calls == 0
    assert await _rows(pool, off) == [] and await _rows(pool, noclient) == []


async def test_missing_tables_do_not_break_the_collector(app, pool):
    import alarms
    await pool.execute("DROP TABLE alarm_events")
    try:
        plc = await _plc(pool, "al9")
        await _source(pool, plc)
        await _collector(pool, {plc: FakeClient([[1, 0]])}).cycle_once(T0)     # без исключения
    finally:
        await alarms.ensure_alarms_schema(pool)


async def test_schema_has_unique_open_index(app, pool):
    idx = await pool.fetchval("SELECT count(*) FROM pg_indexes WHERE tablename = 'alarm_events' AND indexname = 'ux_alarm_events_open'")
    assert idx == 1


# ---------- T-20: снятие после разрыва чтения ----------
async def _gap_flag(pool, plc, bit):
    return await pool.fetchval("SELECT closed_in_gap FROM alarm_events WHERE plc_id = $1 AND bit_index = $2 ORDER BY id DESC LIMIT 1", plc, bit)


async def test_close_after_read_gap_uses_last_seen_time(app, pool):
    plc = await _plc(pool, "gap1")
    await _source(pool, plc)
    err = ConnectionError("нет связи")
    col = _collector(pool, {plc: FakeClient([[0b100000, 0], [0b100000, 0], err, err, err, [0, 0]])})
    await col.cycle_once(T0)
    await col.cycle_once(T0 + timedelta(seconds=1))                    # последнее чтение с битом 5
    await col.cycle_once(T0 + timedelta(seconds=2))
    await col.cycle_once(T0 + timedelta(minutes=10))
    await col.cycle_once(T0 + timedelta(hours=2))
    await col.cycle_once(T0 + timedelta(hours=3))                      # связь вернулась, бит снят
    rows = await _rows(pool, plc)
    assert rows[0]["ended_at"] == T0 + timedelta(seconds=1)
    assert await _gap_flag(pool, plc, 5) is True


async def test_normal_close_has_no_gap_flag(app, pool):
    plc = await _plc(pool, "gap2")
    await _source(pool, plc)
    col = _collector(pool, {plc: FakeClient([[1, 0], [1, 0], [0, 0]])})
    for i in range(3):
        await col.cycle_once(T0 + timedelta(seconds=i))
    assert (await _rows(pool, plc))[0]["ended_at"] == T0 + timedelta(seconds=2)
    assert await _gap_flag(pool, plc, 0) is False


async def test_restart_close_after_downtime_is_marked(app, pool):
    plc = await _plc(pool, "gap3")
    await _source(pool, plc)
    await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at) VALUES ($1, 4, $2)", plc, T0 - timedelta(minutes=3))
    await pool.execute("UPDATE alarm_sources SET last_read_at = $2 WHERE plc_id = $1", plc, T0)
    col = _collector(pool, {plc: FakeClient([[0, 0]])})                # новый сборщик (поллер запущен заново)
    await col.cycle_once(T0 + timedelta(days=2))
    rows = await _rows(pool, plc)
    assert rows[0]["ended_at"] == T0
    assert await _gap_flag(pool, plc, 4) is True


async def test_restart_without_saved_read_time_closes_by_now_unmarked(app, pool):
    plc = await _plc(pool, "gap4")
    await _source(pool, plc)
    await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at) VALUES ($1, 4, $2)", plc, T0)
    await _collector(pool, {plc: FakeClient([[0, 0]])}).cycle_once(T0 + timedelta(hours=1))
    assert (await _rows(pool, plc))[0]["ended_at"] == T0 + timedelta(hours=1)
    assert await _gap_flag(pool, plc, 4) is False


async def test_last_read_at_is_saved_not_every_cycle(app, pool):
    plc = await _plc(pool, "gap5")
    await _source(pool, plc)
    col = _collector(pool, {plc: FakeClient([[0, 0]])})
    await col.cycle_once(T0)
    assert await pool.fetchval("SELECT last_read_at FROM alarm_sources WHERE plc_id = $1", plc) == T0
    await col.cycle_once(T0 + timedelta(seconds=5))                    # слишком рано для новой записи
    assert await pool.fetchval("SELECT last_read_at FROM alarm_sources WHERE plc_id = $1", plc) == T0
    await col.cycle_once(T0 + timedelta(seconds=40))
    assert await pool.fetchval("SELECT last_read_at FROM alarm_sources WHERE plc_id = $1", plc) == T0 + timedelta(seconds=40)


async def test_removed_plc_alarm_is_closed_with_mark_after_return(app, pool):
    plc = await _plc(pool, "gap6")
    await _source(pool, plc)
    clients = {plc: FakeClient([[1, 0]])}
    col = _collector(pool, clients)
    await col.cycle_once(T0)
    clients.pop(plc)                                                   # ПЛК снят с опроса: ничего не меняется
    await col.cycle_once(T0 + timedelta(hours=1))
    assert (await _rows(pool, plc))[0]["ended_at"] is None
    clients[plc] = FakeClient([[0, 0]])                                # вернулся: снятие помечено
    await col.cycle_once(T0 + timedelta(hours=5))
    assert (await _rows(pool, plc))[0]["ended_at"] == T0
    assert await _gap_flag(pool, plc, 0) is True


async def test_read_failure_logs_warning(app, pool, caplog):
    import logging
    plc = await _plc(pool, "gap7")
    await _source(pool, plc)
    err = ConnectionError("нет связи")
    col = _collector(pool, {plc: FakeClient([err])})

    def warns():
        return [r for r in caplog.records if r.levelno == logging.WARNING and "не прочитан" in r.getMessage()]

    with caplog.at_level(logging.WARNING):
        await col.cycle_once(T0)
        assert len(warns()) == 1
        await col.cycle_once(T0 + timedelta(seconds=1))                # без спама
        await col.cycle_once(T0 + timedelta(minutes=2))
        assert len(warns()) == 1
        await col.cycle_once(T0 + timedelta(minutes=6))                # прошло больше 5 минут: напоминание
        assert len(warns()) == 2


async def test_gap_schema_is_idempotent(app, pool):
    import alarms
    await alarms.ensure_alarms_schema(pool)
    await alarms.ensure_alarms_schema(pool)
    cols = {r["column_name"]: r for r in await pool.fetch(
        "SELECT table_name, column_name, column_default FROM information_schema.columns "
        "WHERE (table_name = 'alarm_events' AND column_name = 'closed_in_gap') OR (table_name = 'alarm_sources' AND column_name = 'last_read_at')")}
    assert set(cols) == {"closed_in_gap", "last_read_at"}
    assert "false" in cols["closed_in_gap"]["column_default"].lower()
