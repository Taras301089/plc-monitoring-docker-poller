"""Вкладка «Тренды», график «Связь с ПЛК»: расчёт отрезков, сводка, доступ, период, Excel и экран."""
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
OFF = 5   # KPI_UTC_OFFSET_HOURS по умолчанию
TZ = timezone(timedelta(hours=OFF))
DAY = date(2026, 9, 10)
_n = 0


def at(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


async def _plc(pool, active=True):
    global _n
    _n += 1
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.0.0.1:4840', $2) RETURNING id", f"tr{_n}", active)


async def _ev(pool, plc, state, ts, reason=""):
    await pool.execute("INSERT INTO plc_link_events (plc_id, state, reason, started_at) VALUES ($1, $2, $3, $4)", plc, state, reason, ts)


async def _status(pool, plc, ts):
    await pool.execute(
        "INSERT INTO plc_connection_status (plc_id, is_connected, updated_at) VALUES ($1, TRUE, $2) "
        "ON CONFLICT (plc_id) DO UPDATE SET updated_at = EXCLUDED.updated_at", plc, ts)


@pytest.fixture
def now(monkeypatch):
    """Фиксированное «сейчас» (трендов): 2026-09-11 12:00 по времени завода."""
    import trends
    fixed = at(12, 0, date(2026, 9, 11))
    monkeypatch.setattr(trends, "_now", lambda: fixed)
    return fixed


async def _get(anon, d1=DAY, d2=None):
    r = await anon.get(f"/api/trends/link?from={d1}&to={d2 or d1}")
    assert r.status_code == 200, r.text
    return r.json()


def _plc_data(rep, name):
    return next(p for p in rep["plcs"] if p["name"] == name)


def _hm(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M")


async def test_event_before_period_sets_start_and_loss_inside(anon, pool, now):
    plc = await _plc(pool)
    await _ev(pool, plc, "ok", at(8, 0, DAY - timedelta(days=2)))
    await _ev(pool, plc, "no_link", at(10, 0), "BadTimeout")
    await _ev(pool, plc, "ok", at(10, 30))
    await _status(pool, plc, now)
    p = _plc_data(await _get(anon), f"tr{_n}")
    segs = [(s["state"], _hm(s["start"]), _hm(s["end"]), s["reason"]) for s in p["segments"]]
    assert segs == [("ok", "00:00", "10:00", ""), ("no_link", "10:00", "10:30", "BadTimeout"), ("ok", "10:30", "00:00", "")]
    s = p["summary"]
    assert s["no_link_min"] == 30.0 and s["losses"] == 1
    assert s["pct_ok"] == pytest.approx((24 * 60 - 30) / (24 * 60))


async def test_no_events_before_period_is_unknown_until_first(anon, pool, now):
    plc = await _plc(pool)
    await _ev(pool, plc, "ok", at(6, 0))
    await _status(pool, plc, now)
    p = _plc_data(await _get(anon), f"tr{_n}")
    assert [(s["state"], _hm(s["start"])) for s in p["segments"]] == [("unknown", "00:00"), ("ok", "06:00")]
    assert p["summary"]["unknown_min"] == 360.0
    assert p["summary"]["pct_ok"] == 1.0   # доля считается от времени, по которому есть данные


async def test_loss_started_before_period_is_clipped(anon, pool, now):
    plc = await _plc(pool)
    await _ev(pool, plc, "no_link", at(22, 0, DAY - timedelta(days=1)), "x")
    await _ev(pool, plc, "ok", at(1, 0))
    await _status(pool, plc, now)
    p = _plc_data(await _get(anon), f"tr{_n}")
    assert p["segments"][0]["state"] == "no_link" and _hm(p["segments"][0]["start"]) == "00:00"
    assert p["summary"]["no_link_min"] == 60.0 and p["summary"]["losses"] == 1


async def test_last_segment_ends_now_and_future_is_empty(anon, pool, now):
    plc = await _plc(pool)
    d = now.date()
    await _ev(pool, plc, "ok", at(1, 0, d))
    await _status(pool, plc, now)
    rep = await _get(anon, d)
    p = _plc_data(rep, f"tr{_n}")
    assert _hm(p["segments"][-1]["end"]) == "12:00"


async def test_stale_status_makes_rest_unknown(anon, pool, now):
    plc = await _plc(pool)
    d = now.date()
    await _ev(pool, plc, "ok", at(1, 0, d))
    await _status(pool, plc, at(9, 0, d))   # сборщик остановился в 09:00, сейчас 12:00
    p = _plc_data(await _get(anon, d), f"tr{_n}")
    assert [(s["state"], _hm(s["start"]), _hm(s["end"])) for s in p["segments"]] == [
        ("unknown", "00:00", "01:00"), ("ok", "01:00", "09:00"), ("unknown", "09:00", "12:00")]
    assert p["summary"]["unknown_min"] == 4 * 60.0


async def test_fresh_status_not_cut(anon, pool, now):
    plc = await _plc(pool)
    d = now.date()
    await _ev(pool, plc, "ok", at(0, 0, d))
    await _status(pool, plc, now - timedelta(seconds=100))
    p = _plc_data(await _get(anon, d), f"tr{_n}")
    assert [s["state"] for s in p["segments"]] == ["ok"]


async def test_inactive_plc_flagged_and_order(anon, pool, now):
    a = await _plc(pool)
    b = await _plc(pool, active=False)
    await pool.execute("UPDATE plcs SET sort_order = 2 WHERE id = $1", a)
    await pool.execute("UPDATE plcs SET sort_order = 1 WHERE id = $1", b)
    rep = await _get(anon)
    names = [p["name"] for p in rep["plcs"]]
    assert names.index(f"tr{_n}") < names.index(f"tr{_n - 1}")
    assert _plc_data(rep, f"tr{_n}")["is_active"] is False


async def test_totals(anon, pool, now):
    a, b = await _plc(pool), await _plc(pool)
    for plc in (a, b):
        await _ev(pool, plc, "ok", at(0, 0))
        await _status(pool, plc, now)
    await _ev(pool, a, "no_link", at(5, 0))
    await _ev(pool, a, "ok", at(5, 20))
    await _ev(pool, a, "no_link", at(6, 0))
    await _ev(pool, a, "ok", at(6, 10))
    rep = await _get(anon)
    assert rep["totals"] == {"plcs_failed": 1, "no_link_min": 30.0, "losses": 2}


async def test_period_validation_and_open_access(anon, now):
    assert (await anon.get("/api/trends/link?from=2026-09-10&to=2026-09-09")).status_code == 400
    assert (await anon.get("/api/trends/link?from=2026-01-01&to=2026-05-01")).status_code == 400
    assert (await anon.get("/api/trends/link.xlsx?from=2026-09-10&to=2026-09-09")).status_code == 400
    assert (await anon.get("/api/trends/link?from=2026-07-01&to=2026-09-30")).status_code == 200   # ровно 92 дня


def _xlsx_text(content):
    z = zipfile.ZipFile(BytesIO(content))
    strings = re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)
    strings = ["".join(re.findall(r"<t[^>]*>(.*?)</t>", s, re.S)) for s in strings]
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    return strings, sheet


async def test_excel_name_and_values_match_api(anon, pool, now):
    plc = await _plc(pool)
    await _ev(pool, plc, "ok", at(0, 0))
    await _ev(pool, plc, "no_link", at(10, 0), "BadTimeout")
    await _ev(pool, plc, "ok", at(10, 45))
    await _status(pool, plc, now)
    rep = await _get(anon)
    r = await anon.get(f"/api/trends/link.xlsx?from={DAY}&to={DAY}&src=trends")
    assert r.status_code == 200
    fname = unquote(re.search(r"filename\*=UTF-8''(.+)", r.headers["content-disposition"]).group(1))
    assert fname == f"Тренды_связь_{DAY}.xlsx"
    strings, sheet = _xlsx_text(r.content)
    assert "Связь с ПЛК, период 10.09.2026" in strings and "BadTimeout" in strings and f"tr{_n}" in strings
    assert "Потери связи" in strings and "По ПЛК" in strings
    assert "SUM(D8:D" in sheet and "SUM(E8:E" in sheet
    assert rep["totals"]["no_link_min"] == 45.0
    assert "<v>45</v>" in sheet
    r2 = await anon.get(f"/api/trends/link.xlsx?from={DAY}&to={DAY + timedelta(days=2)}&src=trends")
    f2 = unquote(re.search(r"filename\*=UTF-8''(.+)", r2.headers["content-disposition"]).group(1))
    assert f2 == f"Тренды_связь_{DAY}_{DAY + timedelta(days=2)}.xlsx"


def test_screen_static_checks():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "trends.js").read_text(encoding="utf-8")
    ref = (STATIC / "reference.html").read_text(encoding="utf-8")
    assert 'data-tab="trends"' in index and "📉 Тренды" in index and 'id="trends"' in index
    assert index.index('data-tab="charts"') < index.index('data-tab="trends"') < index.index('data-tab="reports"')
    assert "/static/trends.js" in index
    assert "index.html#trends" in ref
    for word in ("День", "Неделя", "Месяц", "Прошлый месяц", "tr-from", "tr-to", "◀", "▶", "⬇ Excel", "Связь с ПЛК"):
        assert word in js, word
    assert "AbortSignal.timeout(6000)" in js
    assert "/api/trends/link" in js and "src=trends" in js
    assert "failStreak >= 2" in js
    assert js.count("title=") >= 8
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", js)   # цвета только через переменные
