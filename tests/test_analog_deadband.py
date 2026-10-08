"""Запись аналоговых значений с мёртвой зоной: не при каждом опросе, а при изменении, раз в минуту и во время просадки."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "python-async-poller"))

from checker import TagChecker  # noqa: E402
from models import Tag, TagReading  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def _tag(tag_id=1, **kw):
    return Tag(id=tag_id, plc_id=8, name=f"U{tag_id}", node_id=f"ns=3;s=U{tag_id}", tag_type="ANALOG", is_alarm_enabled=False, **kw)


def _feed(checker, tag, values, step_sec=1.0, start=0.0):
    """Подаёт значения как опросы раз в step_sec; возвращает список записанных (секунда, значение)."""
    written = []
    for i, v in enumerate(values):
        sec = start + i * step_sec
        telemetry, *_ = checker.process([TagReading(tag=tag, value=v, source_ts=T0 + timedelta(seconds=sec), is_good=True)])
        written += [(sec, p.value) for p in telemetry]
    return written


def test_without_deadband_every_poll_is_written_as_before():
    w = _feed(TagChecker(), _tag(), [228.0, 228.0, 228.1, 228.1])
    assert len(w) == 4


def test_first_value_is_written():
    assert _feed(TagChecker(), _tag(deadband=1.0), [228.0]) == [(0.0, 228.0)]


def test_small_changes_are_not_written():
    w = _feed(TagChecker(), _tag(deadband=1.0), [228.0, 228.4, 227.6, 228.9, 228.2])
    assert w == [(0.0, 228.0)]


def test_change_not_less_than_deadband_is_written_and_becomes_new_base():
    w = _feed(TagChecker(), _tag(deadband=1.0), [228.0, 228.5, 229.0, 229.4, 230.1])
    assert [v for _, v in w] == [228.0, 229.0, 230.1]


def test_heartbeat_every_minute_even_if_unchanged():
    w = _feed(TagChecker(), _tag(deadband=1.0), [228.0] * 130)         # 130 опросов раз в секунду
    assert [s for s, _ in w] == [0.0, 60.0, 120.0]


def test_below_limit_is_written_on_every_poll_until_recovery():
    w = _feed(TagChecker(), _tag(deadband=1.0, limit_low=207.0), [228.0, 206.0, 206.1, 205.0, 228.0, 228.2])
    assert [v for _, v in w] == [228.0, 206.0, 206.1, 205.0, 228.0]    # просадка пишется каждый опрос, возврат тоже


def test_above_limit_is_written_on_every_poll():
    w = _feed(TagChecker(), _tag(deadband=1.0, limit_high=253.0), [230.0, 254.0, 254.1, 230.0])
    assert [v for _, v in w] == [230.0, 254.0, 254.1, 230.0]


def test_tags_are_independent():
    c = TagChecker()
    a, b = _tag(1, deadband=1.0), _tag(2, deadband=1.0)
    readings = lambda va, vb, s: [TagReading(tag=a, value=va, source_ts=T0 + timedelta(seconds=s), is_good=True),
                                  TagReading(tag=b, value=vb, source_ts=T0 + timedelta(seconds=s), is_good=True)]
    assert len(c.process(readings(228.0, 100.0, 0))[0]) == 2
    out = c.process(readings(228.2, 105.0, 1))[0]
    assert [p.tag_id for p in out] == [2]


def test_bad_quality_is_skipped_and_state_kept():
    c, t = TagChecker(), _tag(deadband=1.0)
    _feed(c, t, [228.0])
    assert c.process([TagReading(tag=t, value=0.0, source_ts=T0 + timedelta(seconds=1), is_good=False)])[0] == []
    assert _feed(c, t, [228.3], start=2.0) == []


def test_dropped_tag_state_is_forgotten():
    c, t = TagChecker(), _tag(deadband=1.0)
    _feed(c, t, [228.0])
    c.drop_tag(t.id)
    assert _feed(c, t, [228.0], start=1.0) == [(1.0, 228.0)]
    c.retain_tags(set())
    assert _feed(c, t, [228.0], start=2.0) == [(2.0, 228.0)]


async def test_thresholds_loaded_from_plc_tags(app, pool):
    from database import Database
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('dbplc', 'opc.tcp://10.0.0.1:4840') RETURNING id")
    await pool.execute(
        "INSERT INTO plc_tags (plc_id, name, node_id, tag_type, deadband, limit_low, limit_high) "
        "VALUES ($1, 'U', 'ns=3;s=U', 'ANALOG', 1.0, 207, 253)", plc)
    db = object.__new__(Database)
    db._pool = pool
    tag = (await db.load_tags(plc))[0]
    assert (tag.deadband, tag.limit_low, tag.limit_high) == (1.0, 207.0, 253.0)


async def test_old_schema_without_columns_still_loads_tags(app, pool):
    """Поллер мог стартовать раньше API (колонок ещё нет): теги всё равно загружаются, мёртвая зона не задана."""
    import trends
    from database import Database
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('oldplc', 'opc.tcp://10.0.0.1:4840') RETURNING id")
    await pool.execute("INSERT INTO plc_tags (plc_id, name, node_id, tag_type) VALUES ($1, 'U', 'ns=3;s=U', 'ANALOG')", plc)
    await pool.execute("ALTER TABLE plc_tags DROP COLUMN deadband, DROP COLUMN limit_low, DROP COLUMN limit_high")
    try:
        db = object.__new__(Database)
        db._pool = pool
        tag = (await db.load_tags(plc))[0]
        assert (tag.deadband, tag.limit_low, tag.limit_high) == (None, None, None)
    finally:
        await trends.ensure_trends_schema(pool)


async def test_ensure_schema_adds_columns_and_is_repeatable(app, pool):
    import trends
    await trends.ensure_trends_schema(pool)
    await trends.ensure_trends_schema(pool)
    cols = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'plc_tags'")}
    assert {"deadband", "limit_low", "limit_high"} <= cols
