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
    assert rows[1]["ended_at"] == T0 + timedelta(minutes=5)


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
