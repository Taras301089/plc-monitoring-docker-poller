"""Аварии самого ПЛК (alarm_ac_events) на экране «Аварии»: журнал, фильтр класса, счётчики, аналитика, Excel, кнопка «Убрать»."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_alarms_analytics import P, _table, read_xlsx as read_an_xlsx
from test_alarms_journal import _cat, _ev, _utc, read_xlsx

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "alarms.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
EP = "opc.tcp://10.1.2.9:4840"


def _a():
    import alarms
    return alarms


def node(expr):
    code = f"const u=require({json.dumps(str(STATIC / 'alarms.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


async def _plc(pool, name, ac=True):
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active, ac_alarms) VALUES ($1, $2, TRUE, $3) RETURNING id", name, EP, ac)


async def _ac(pool, plc, cond, source, message, started, ended=None, acked=None, at_start=False, name=""):
    return await pool.fetchval(
        "INSERT INTO alarm_ac_events (plc_id, condition_id, source, condition_name, message, severity, started_at, ended_at, acked_at, active_at_start) "
        "VALUES ($1, $2, $3, $4, $5, 500, $6, $7, $8, $9) RETURNING id", plc, cond, source, name, message, started, ended, acked, at_start)


async def _mixed(pool):
    """ПЛК с одной аварией-битом (активна, снятая) и тремя авариями ПЛК: активная, снятая с квитированием, без станции."""
    plc = await _plc(pool, "Changan")
    await _cat(pool, plc, 1, "UB20 body-detail sensor is blocked", "101", "Warning", "UB20", "")
    now = datetime.now(timezone.utc)
    await _ev(pool, plc, 1, now - timedelta(hours=1))
    await _ev(pool, plc, 1, _utc(2026, 3, 10, 1, 0), _utc(2026, 3, 10, 1, 10))
    await _ac(pool, plc, "ns=3;s=c1", "UB030_ET200_IM1", "Ошибка модуля ET200", now - timedelta(minutes=30), at_start=True)
    await _ac(pool, plc, "ns=3;s=c2", "MB110_VFD1", "Перегрев привода", _utc(2026, 3, 10, 2, 0), _utc(2026, 3, 10, 2, 20),
              acked=_utc(2026, 3, 10, 2, 5))
    await _ac(pool, plc, "ns=3;s=c3", "PN-IO", "", _utc(2026, 3, 10, 3, 0), _utc(2026, 3, 10, 3, 1), name="Profinet lost")
    return plc


async def _get(anon, plc, **kw):
    return await anon.get("/api/alarms/events", params={"plc_id": plc, **kw})


# ------------------------------------------------------------------ журнал
async def test_journal_has_both_sources(anon, pool):
    plc = await _mixed(pool)
    j = (await _get(anon, plc, state="all")).json()
    assert j["counts"] == {"active": 2, "history": 3} and j["total"] == 5
    ac = {i["condition_id"]: i for i in j["items"] if i["origin"] == "ac"}
    bits = [i for i in j["items"] if i["origin"] == "bit"]
    assert len(ac) == 3 and len(bits) == 2 and all(i["source"] == "HMI" and i["condition_id"] is None for i in bits)
    a1 = ac["ns=3;s=c1"]
    assert (a1["source"], a1["class"], a1["code"], a1["text"]) == ("ПЛК", "диагностика", "", "Ошибка модуля ET200")
    assert a1["station"] == "UB30" and a1["device"] == "UB030_ET200_IM1" and a1["bit_index"] is None
    assert a1["active"] and a1["ended_at"] is None and a1["active_at_start"] is True and 1750 <= a1["duration_sec"] <= 1900
    a2 = ac["ns=3;s=c2"]
    assert a2["station"] == "MB110" and a2["duration_sec"] == 1200 and a2["duration_text"] == "20 мин 0 с"
    assert a2["started_at"] == "2026-03-10T02:00:00Z" and a2["acked_at"] == "2026-03-10T02:05:00Z" and not a2["active"]
    a3 = ac["ns=3;s=c3"]
    assert a3["station"] == "" and a3["text"] == "Profinet lost"       # станция не найдена: на экране «—»; пустое сообщение: имя условия
    # «Активные» по умолчанию: обе активные, новые сверху
    act = (await _get(anon, plc)).json()
    assert [i["origin"] for i in act["items"]] == ["ac", "bit"]


async def test_journal_filters_sort_and_counts(anon, pool):
    plc = await _mixed(pool)
    g = lambda **kw: _get(anon, plc, state="all", **kw)  # noqa: E731
    j = (await g(**{"class": "ПЛК · диагностика"})).json()
    assert j["total"] == 3 and {i["origin"] for i in j["items"]} == {"ac"}
    assert j["counts"] == {"active": 1, "history": 2}
    j = (await g(**{"class": "Warning"})).json()
    assert j["total"] == 2 and {i["origin"] for i in j["items"]} == {"bit"}
    assert (await g(station="MB110")).json()["total"] == 1
    assert (await g(device="PN-IO")).json()["total"] == 1
    assert (await g(q="перегрев")).json()["total"] == 1
    st = (await g(sort="station", dir="asc")).json()["items"]
    assert [i["station"] for i in st] == ["MB110", "UB20", "UB20", "UB30", ""]
    ids = [(i["origin"], i["id"]) for i in (await g(sort="started_at", dir="asc", limit=2)).json()["items"]] + \
          [(i["origin"], i["id"]) for i in (await g(sort="started_at", dir="asc", limit=2, offset=2)).json()["items"]] + \
          [(i["origin"], i["id"]) for i in (await g(sort="started_at", dir="asc", limit=2, offset=4)).json()["items"]]
    assert len(set(ids)) == 5
    h = (await _get(anon, plc, state="history", **{"from": "2026-03-10", "to": "2026-03-10"})).json()
    assert h["total"] == 3 and h["counts"] == {"active": 2, "history": 3}


async def test_filters_endpoint_includes_plc_alarms(anon, pool):
    plc = await _mixed(pool)
    f = (await anon.get("/api/alarms/filters", params={"plc_id": plc})).json()
    assert f["stations"] == ["MB110", "UB20", "UB30"]
    assert set(f["devices"]) == {"UB030_ET200_IM1", "MB110_VFD1", "PN-IO"}
    assert f["classes"] == ["Warning", "ПЛК · диагностика"] and f["configured"] is True
    # ПЛК только с флажком (событий ещё нет, словаря нет): журнал доступен в выборе
    only = await _plc(pool, "ChanganNew")
    f = (await anon.get("/api/alarms/filters", params={"plc_id": only})).json()
    assert f == {"stations": [], "devices": [], "classes": ["ПЛК · диагностика"], "configured": True}
    # без словаря и без аварий ПЛК: не настроен
    none = await _plc(pool, "Empty", ac=False)
    f = (await anon.get("/api/alarms/filters", params={"plc_id": none})).json()
    assert f["configured"] is False and f["classes"] == []


async def test_journal_excel_has_plc_alarms(anon, pool):
    plc = await _mixed(pool)
    r = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "all"})
    assert r.status_code == 200
    _, rows = read_xlsx(r.content)
    ac = [c for c in rows if c.get("G") == "ПЛК"]
    assert len(ac) == 3 and all(c["H"] == "диагностика" for c in ac)
    assert {c["F"] for c in ac} == {"Ошибка модуля ET200", "Перегрев привода", "Profinet lost"}
    assert rows[-1]["A"] == "Итого записей" and rows[-1]["B"] == "5"


# ------------------------------------------------------------------ аналитика
async def _an_setup(pool):
    plc = await _plc(pool, "Changan")
    await _cat(pool, plc, 1, "UB20 sensor blocked", "101", "Alarm", "UB20", "")
    await _ev(pool, plc, 1, P("2026-03-10", 8, 0), P("2026-03-10", 8, 10))                         # бит: 10 мин
    await _ac(pool, plc, "c1", "UB030_ET200", "Ошибка модуля", P("2026-03-10", 9, 0), P("2026-03-10", 9, 30))   # 30 мин
    await _ac(pool, plc, "c1", "UB030_ET200", "Ошибка модуля", P("2026-03-10", 10, 0), P("2026-03-10", 10, 15))  # 15 мин
    await _ac(pool, plc, "c2", "PN-IO", "Нет связи", P("2026-03-10", 11, 0), P("2026-03-10", 11, 5))             # 5 мин, без станции
    return plc


async def test_analytics_counts_plc_alarms(anon, pool):
    plc = await _an_setup(pool)
    j = (await anon.get("/api/alarms/analytics", params={"brand": "Changan", "from": "2026-03-10", "to": "2026-03-10"})).json()
    assert j["tiles"] == {"events": 4, "total_sec": 60 * 60, "distinct": 3}
    assert j["scope"]["plcs_with_data"] == 1
    top = j["frequent"][0]
    assert (top["text"], top["code"], top["station"], top["count"], top["origin"], top["condition_id"], top["bit_index"]) == \
        ("Ошибка модуля", "—", "UB30", 2, "ac", "c1", None)
    assert j["pareto"]["count"][0]["label"] == "Ошибка модуля" and j["pareto"]["time"][0]["value"] == 45.0
    st = {s["station"]: s for s in j["stations"]}
    assert st["UB30"]["count"] == 2 and st["UB30"]["total_sec"] == 45 * 60 and st["—"]["count"] == 1 and st["UB20"]["count"] == 1
    assert sum(s["count"] for s in j["stations"]) == 4
    assert any(c["origin"] == "ac" and c["code"] == "—" for c in j["cases"])
    assert j["dynamics"][-1] == {"date": "2026-03-10", "count": 4, "total_sec": 3600}
    # ПЛК только с флажком и без событий: есть данные (пустые), а не «нет словаря»
    only = await _plc(pool, "Solo")
    j = (await anon.get("/api/alarms/analytics", params={"plc_id": only, "from": "2026-03-10", "to": "2026-03-10"})).json()
    assert j["scope"]["plcs_with_data"] == 1
    assert plc


async def test_analytics_excel_has_plc_alarms(anon, pool):
    await _an_setup(pool)
    r = await anon.get("/api/alarms/analytics.xlsx", params={"brand": "Changan", "from": "2026-03-10", "to": "2026-03-11"})
    assert r.status_code == 200
    _, rows = read_an_xlsx(r.content)
    tl = next(i for i, c in enumerate(rows) if c.get("A") == "Событий")
    assert rows[tl + 1]["A"] == "4" and rows[tl + 1]["C"] == "3"
    _, data = _table(rows, "Самые частые")
    first = data[0]
    assert (first["C"], first["D"], first["B"], first["E"]) == ("—", "Ошибка модуля", "UB30", "2")
    _, data = _table(rows, "По станциям")
    assert {d["B"] for d in data} == {"UB30", "UB20", "—"} and sum(int(d["C"]) for d in data) == 4


async def test_station_exclusion_works_for_plc_alarms(users, pool):
    plc = await _an_setup(pool)
    c = users["t_master"]
    r = await c.post("/api/alarms/exclusions", json={"plc_id": plc, "kind": "station", "station": "UB30"})
    assert r.status_code == 200, r.text
    j = (await c.get("/api/alarms/analytics", params={"brand": "Changan", "from": "2026-03-10", "to": "2026-03-10"})).json()
    assert j["tiles"]["events"] == 2 and j["removed"]["events"] == 2
    assert (await c.post("/api/alarms/exclusions", json={"plc_id": plc, "kind": "station", "station": "XX99"})).status_code == 422


def test_build_analytics_ac_key_is_condition():
    a = _a()
    t = lambda h: datetime(2026, 3, 10, h, tzinfo=timezone.utc)  # noqa: E731
    ac = lambda cond, h: {"plc_id": 1, "plc": "X", "bit_index": None, "condition_id": cond, "origin": "ac", "started_at": t(h),  # noqa: E731
                          "ended_at": t(h) + timedelta(minutes=1), "code": "", "text": "m", "station": "", "in_catalog": True}
    bit = {"plc_id": 1, "plc": "X", "bit_index": 7, "started_at": t(5), "ended_at": t(5) + timedelta(minutes=1), "code": "7",
           "text": "b", "station": "", "in_catalog": True}
    # одинаковый текст, разные условия: разные аварии; авария-бит с тем же номером в исключениях не задевает аварии ПЛК
    r = a.build_analytics([ac("c1", 1), ac("c2", 2), ac("c1", 3), bit], None, t(23), True, {"alarms": {(1, 7)}, "stations": set()})
    assert r["tiles"]["distinct"] == 2 and r["tiles"]["events"] == 3 and r["removed_events"] == 1
    assert {(x["condition_id"], x["count"]) for x in r["frequent"]} == {("c1", 2), ("c2", 1)}


# ------------------------------------------------------------------ экран
def test_no_remove_button_for_plc_alarms_and_titles():
    assert "anCanRemoveAlarm(r)" in JS and "EX_AC_TIP" in JS
    assert "filtersConfigured(f)" in JS and "ackedTitle(r.acked_at" in JS
    for m in re.finditer(r"<(button|select|input)\b[^>]*>", JS):
        assert "title=" in m.group(0), m.group(0)


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_helpers():
    assert node("u.anCanRemoveAlarm({origin:'ac'})") is False
    assert node("u.anCanRemoveAlarm({origin:'bit', bit_index: 3})") is True
    assert node("u.anCanRemoveAlarm({bit_index: 3})") is True
    assert node("u.filtersConfigured({stations:[], configured:true})") is True
    assert node("u.filtersConfigured({stations:['UB10']})") is True
    assert node("u.filtersConfigured({stations:[], configured:false})") is False
    assert node("u.ackedTitle('2026-03-10T02:05:00Z', 5)") == "Квитирована на ПЛК: 10.03.2026, 07:05:00"


def test_reference_mentions_plc_alarms_on_screen():
    ref = (STATIC / "reference.html").read_text(encoding="utf-8")
    li = re.search(r'<li data-date="2026-10-15">аварии самого ПЛК.*?</li>', ref, re.S).group(0)
    assert "Аварии ПЛК видны на экране «Аварии»" in li and "ПЛК · диагностика" in li
