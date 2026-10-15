"""Убрать аварию или станцию из статистики аналитики аварий: схема, API и права, расчёт, возврат, Excel, журнал, JS."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from test_alarms_analytics import (NODE, JS, STATIC, _a, _cat, _ev, _get, _plc, _table, _worktime_setup, node, P, read_xlsx)

BASE = "/api/alarms/exclusions"


async def _two_stations(pool):
    """GWM_MB: станция MB100 (биты 1 и 2, бит 1 дважды) и MB110 (бит 3); все события в один день."""
    plc = await _plc(pool, "GWM_MB")
    await _cat(pool, plc, 1, "MB100_W32L_C4.5 clamping timeout", "7413", "MB100")
    await _cat(pool, plc, 2, "MB100 door open", "7414", "MB100")
    await _cat(pool, plc, 3, "MB110 sensor blocked", "7500", "MB110")
    await _ev(pool, plc, 1, P("2026-03-10", 9, 0), P("2026-03-10", 9, 10))
    await _ev(pool, plc, 1, P("2026-03-10", 9, 30), P("2026-03-10", 9, 50))
    await _ev(pool, plc, 2, P("2026-03-10", 10, 0), P("2026-03-10", 10, 5))
    await _ev(pool, plc, 3, P("2026-03-11", 11, 0), P("2026-03-11", 11, 30))
    return plc


async def _day(client, **kw):
    kw.setdefault("all_hours", "true")
    return (await _get(client, **kw)).json()


# ------------------------------------------------------------------ схема
async def test_schema_and_unique_active(app, pool):
    plc = await _plc(pool, "GWM_UB")
    cols = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'alarm_exclusions'")}
    assert {"id", "plc_id", "kind", "bit_index", "station", "reason", "created_by", "created_name", "created_at",
            "restored_at", "restored_by", "restored_name"} <= cols
    ins = "INSERT INTO alarm_exclusions (plc_id, kind, bit_index, station) VALUES ($1, $2, $3, $4)"
    await pool.execute(ins, plc, "alarm", 5, None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(ins, plc, "alarm", 5, None)
    await pool.execute(ins, plc, "station", None, "UB20")
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(ins, plc, "station", None, "UB20")
    await pool.execute("UPDATE alarm_exclusions SET restored_at = now() WHERE kind = 'alarm'")
    await pool.execute(ins, plc, "alarm", 5, None)           # после возврата можно убрать снова: история сохраняется
    assert await pool.fetchval("SELECT count(*) FROM alarm_exclusions WHERE kind = 'alarm'") == 2
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(ins, plc, "alarm", None, None)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(ins, plc, "station", None, None)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(ins, plc, "other", 1, None)


async def test_schema_is_idempotent(app, pool):
    import alarms
    await alarms.ensure_alarms_schema(pool)
    await alarms.ensure_alarms_schema(pool)


# ------------------------------------------------------------------ права
async def test_rights(anon, users, pool):
    plc = await _plc(pool, "GWM_MB")
    body = {"plc_id": plc, "kind": "alarm", "bit_index": 7}
    assert (await anon.get(BASE)).status_code == 200                         # чтение открыто
    assert (await anon.post(BASE, json=body)).status_code == 401
    r = await users["t_viewer"].post(BASE, json=body)
    assert r.status_code == 403
    assert await pool.fetchval("SELECT count(*) FROM alarm_exclusions") == 0
    for login in ("t_admin", "t_chief", "t_area", "t_master", "t_oto"):
        r = await users[login].post(BASE, json={**body, "bit_index": 10 + len(login)})
        assert r.status_code == 200, (login, r.text)
    eid = (await users["t_master"].post(BASE, json=body)).json()["item"]["id"]
    assert (await anon.delete(f"{BASE}/{eid}")).status_code == 401
    assert (await users["t_viewer"].delete(f"{BASE}/{eid}")).status_code == 403
    assert await pool.fetchval("SELECT restored_at FROM alarm_exclusions WHERE id = $1", eid) is None
    assert (await users["t_area"].delete(f"{BASE}/{eid}")).status_code == 200      # вернуть может любой, кроме «Просмотр»


async def test_catalog_import_still_admin_only(anon, users, pool):
    plc = await _plc(pool, "GWM_MB")
    assert (await users["t_master"].post(f"/api/alarms/catalog/import?plc_id={plc}", content=b"x")).status_code == 403
    assert (await anon.post(f"/api/alarms/catalog/import?plc_id={plc}", content=b"x")).status_code == 401


# ------------------------------------------------------------------ создание, повтор, валидация, возврат
async def test_add_repeat_and_author(users, pool):
    plc = await _two_stations(pool)
    c = users["t_master"]
    r = await c.post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1, "reason": " станция не используется "})
    assert r.status_code == 200 and r.json()["created"] is True
    it = r.json()["item"]
    assert it["kind"] == "alarm" and it["bit_index"] == 1 and it["code"] == "7413" and it["station"] == "MB100"
    assert it["text"].startswith("MB100_W32L") and it["reason"] == "станция не используется" and it["active"] is True
    assert it["created_name"] == "T_MASTER, Мастер" and it["plc"] == "GWM_MB"
    again = await c.post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1})
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["item"]["id"] == it["id"]
    st = await c.post(BASE, json={"plc_id": plc, "kind": "station", "station": "MB110"})
    assert st.status_code == 200 and st.json()["item"]["station"] == "MB110" and st.json()["item"]["bit_index"] is None
    st2 = await c.post(BASE, json={"plc_id": plc, "kind": "station", "station": "MB110"})
    assert st2.json()["item"]["id"] == st.json()["item"]["id"]
    assert await pool.fetchval("SELECT count(*) FROM alarm_exclusions") == 2


@pytest.mark.parametrize("body,code", [
    ({"kind": "other", "bit_index": 1}, 422),
    ({"kind": "alarm"}, 422),
    ({"kind": "alarm", "bit_index": -1}, 422),
    ({"kind": "alarm", "bit_index": 80000}, 422),
    ({"kind": "station"}, 422),
    ({"kind": "station", "station": "   "}, 422),
    ({"kind": "station", "station": "XX99"}, 422),          # такой станции нет в словаре этого ПЛК
])
async def test_validation(users, pool, body, code):
    plc = await _two_stations(pool)
    r = await users["t_master"].post(BASE, json={"plc_id": plc, **body})
    assert r.status_code == code, r.text
    assert await pool.fetchval("SELECT count(*) FROM alarm_exclusions") == 0


async def test_validation_unknown_plc_and_foreign_station(users, pool):
    plc = await _two_stations(pool)
    other = await _plc(pool, "GWM_UB")
    assert (await users["t_master"].post(BASE, json={"plc_id": 999999, "kind": "alarm", "bit_index": 1})).status_code == 404
    r = await users["t_master"].post(BASE, json={"plc_id": other, "kind": "station", "station": "MB100"})   # станция другого ПЛК
    assert r.status_code == 422 and "MB100" in r.json()["detail"]
    assert plc != other


async def test_restore_keeps_history(users, anon, pool):
    plc = await _two_stations(pool)
    eid = (await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1})).json()["item"]["id"]
    r = await users["t_chief"].delete(f"{BASE}/{eid}")
    assert r.status_code == 200 and r.json()["item"]["active"] is False
    row = await pool.fetchrow("SELECT * FROM alarm_exclusions WHERE id = $1", eid)
    assert row is not None and row["restored_at"] is not None and row["restored_name"] == "T_CHIEF, Начальник цеха"
    assert row["created_name"] == "T_MASTER, Мастер"
    again = await users["t_chief"].delete(f"{BASE}/{eid}")
    assert again.status_code == 409 and "Уже возвращено" in again.json()["detail"]
    assert (await users["t_chief"].delete(f"{BASE}/999999")).status_code == 404
    listed = (await anon.get(BASE)).json()
    assert [i["id"] for i in listed["items"]] == [eid] and listed["active"] == 0 and listed["items"][0]["restored_at"]
    assert (await anon.get(BASE, params={"active_only": "true"})).json()["items"] == []
    # убрать снова: новая запись, старая остаётся в журнале
    r = await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1})
    assert r.json()["created"] is True and r.json()["item"]["id"] != eid
    assert await pool.fetchval("SELECT count(*) FROM alarm_exclusions") == 2
    assert (await anon.get(BASE, params={"plc_id": plc, "active_only": "true"})).json()["active"] == 1


# ------------------------------------------------------------------ расчёт: чистая функция
def _event(plc_id, bit, start, end, text="авария", station="UB20", code="1"):
    return {"plc_id": plc_id, "plc": "X", "bit_index": bit, "started_at": start, "ended_at": end, "code": code,
            "text": text, "station": station, "in_catalog": True}


def _sample():
    t = lambda h, m=0: datetime(2026, 3, 10, h, m, tzinfo=timezone.utc)  # noqa: E731
    return [_event(1, 1, t(1), t(1, 10), "A", "MB100"), _event(1, 1, t(2), t(2, 20), "A", "MB100"),
            _event(1, 2, t(3), t(3, 5), "B", "MB100"), _event(1, 3, t(4), t(5, 40), "C", "UB20"),
            _event(2, 1, t(6), t(6, 10), "A2", "MB100"), _event(1, 9, t(7), t(7, 1), "D", "")], t(23)


def test_build_alarm_excluded_everywhere():
    a = _a()
    evs, now = _sample()
    r = a.build_analytics(evs, None, now, True, {"alarms": {(1, 1)}, "stations": set()})
    assert r["removed_events"] == 2
    assert r["tiles"] == {"events": 4, "total_sec": (5 + 100 + 10 + 1) * 60, "distinct": 4}
    for key in ("frequent", "longest"):
        assert "A" not in [x["text"] for x in r[key]] and "A2" in [x["text"] for x in r[key]]
    assert all(c["text"] != "A" for c in r["cases"])
    assert all(p["label"] != "A" for p in r["pareto"]["count"] + r["pareto"]["time"])
    st = {(x["plc_id"], x["station"]): x for x in r["stations"]}
    assert st[(1, "MB100")]["count"] == 1 and st[(2, "MB100")]["count"] == 1           # B (ПЛК 1) и A2 (ПЛК 2, тот же бит) остались
    assert sum(x["count"] for x in r["stations"]) == r["tiles"]["events"]
    assert sum(x["total_sec"] for x in r["stations"]) == r["tiles"]["total_sec"]
    assert r["days"][0]["count"] == 4
    assert sum(p["share"] for p in r["pareto"]["count"]) == pytest.approx(1.0, abs=1e-4)


def test_build_station_excluded_everywhere():
    a = _a()
    evs, now = _sample()
    r = a.build_analytics(evs, None, now, True, {"alarms": set(), "stations": {(1, "MB100")}})
    assert r["removed_events"] == 3                   # A, A, B на ПЛК 1; A2 на ПЛК 2 остаётся
    assert r["tiles"]["events"] == 3 and r["tiles"]["distinct"] == 3
    assert {x["text"] for x in r["frequent"]} == {"C", "A2", "D"}
    assert ("X", "MB100") in {(x["plc"], x["station"]) for x in r["stations"]}      # это станция ПЛК 2
    assert sum(x["count"] for x in r["stations"]) == 3


def test_build_no_exclusions_is_unchanged():
    a = _a()
    evs, now = _sample()
    base = a.build_analytics(evs, None, now, True)
    assert base["removed_events"] == 0
    same = a.build_analytics(evs, None, now, True, {"alarms": set(), "stations": set()})
    assert base == same
    assert base["tiles"]["events"] == 6


def test_station_dash_is_not_excludable_and_work_time_not_counted_twice():
    a = _a()
    t = lambda h, m=0: datetime(2026, 3, 10, h, m, tzinfo=timezone.utc)  # noqa: E731
    work = {1: [(t(10), t(12))]}
    evs = [_event(1, 1, t(9), t(9, 30), station="MB100"),       # убрана и вне смены: считается как убранная, не как «вне смены»
           _event(1, 2, t(9), t(9, 30), station="UB20"),        # вне смены
           _event(1, 3, t(10), t(10, 10), station="UB20"),
           _event(1, 4, t(10, 20), t(10, 30), station="")]
    r = a.build_analytics(evs, work, t(20), False, {"alarms": set(), "stations": {(1, "MB100"), (1, "")}})
    assert r["removed_events"] == 1 and r["excluded"] == 1 and r["tiles"]["events"] == 2


# ------------------------------------------------------------------ расчёт: API, период, ПЛК, переключатель «вне смены»
async def test_api_alarm_removed_and_restored(users, anon, pool):
    plc = await _two_stations(pool)
    full = await _day(anon, brand="GWM")
    assert full["tiles"]["events"] == 4 and full["removed"] == {"alarms": 0, "stations": 0, "events": 0, "items": []}
    assert full["excluded"] == 0                                    # прежний смысл ключа excluded (вне смены) не меняется
    eid = (await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1})).json()["item"]["id"]
    j = await _day(anon, brand="GWM")
    assert j["tiles"] == {"events": 2, "total_sec": 35 * 60, "distinct": 2}
    assert [x["code"] for x in j["frequent"]] == ["7500", "7414"] or {x["code"] for x in j["frequent"]} == {"7414", "7500"}
    assert j["removed"]["alarms"] == 1 and j["removed"]["stations"] == 0 and j["removed"]["events"] == 2
    it = j["removed"]["items"][0]
    assert it["id"] == eid and it["created_name"] == "T_MASTER, Мастер" and it["created_at"] and it["code"] == "7413"
    assert [d["count"] for d in j["days"]] == [1, 1]                # 10.03: остался бит 2; 11.03: бит 3
    await users["t_master"].delete(f"{BASE}/{eid}")
    back = await _day(anon, brand="GWM")
    assert back["tiles"] == full["tiles"] and back["frequent"] == full["frequent"] and back["stations"] == full["stations"]
    assert back["removed"]["items"] == [] and back["removed"]["events"] == 0


async def test_api_station_removed_all_periods_and_other_plc(users, anon, pool):
    plc = await _two_stations(pool)
    other = await _plc(pool, "Chery_MB")
    await _cat(pool, other, 1, "MB100 other plc alarm", "1", "MB100")
    await _ev(pool, other, 1, P("2026-03-10", 9, 0), P("2026-03-10", 9, 10))
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "station", "station": "MB100"})
    j = await _day(anon, plc_id=plc)
    assert j["tiles"]["events"] == 1 and {s["station"] for s in j["stations"]} == {"MB110"}
    assert j["removed"] == {**j["removed"], "alarms": 0, "stations": 1, "events": 4 - 1}
    assert (await _day(anon, plc_id=other))["tiles"]["events"] == 1             # станция MB100 другого ПЛК не затронута
    assert (await _day(anon))["tiles"]["events"] == 2
    # действует на все периоды: и на прошлые, и на будущие
    one = await _day(anon, plc_id=plc, **{"from": "2026-03-10", "to": "2026-03-10"})
    assert one["tiles"]["events"] == 0 and one["frequent"] == [] and one["pareto"] == {"count": [], "time": []}
    await _ev(pool, plc, 1, P("2026-03-12", 9, 0), P("2026-03-12", 9, 5))
    assert (await _day(anon, plc_id=plc, **{"to": "2026-03-12"}))["tiles"]["events"] == 1


async def test_api_toggle_all_hours_and_worktime(users, anon, pool):
    plc = await _worktime_setup(pool)
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "station", "station": "UB30"})
    j = (await _get(anon, brand="GWM")).json()
    assert j["tiles"] == {"events": 2, "total_sec": 1200, "distinct": 1}
    assert j["excluded"] == 2 and j["removed"]["events"] == 2                   # события UB30 убраны сразу, остальные «вне смены» как раньше
    allj = (await _get(anon, brand="GWM", all_hours="true")).json()
    assert allj["tiles"]["events"] == 4 and allj["excluded"] == 0 and allj["removed"]["events"] == 2


async def test_journal_stays_complete(users, anon, pool):
    plc = await _two_stations(pool)
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "station", "station": "MB100"})
    ev = (await anon.get("/api/alarms/events", params={"plc_id": plc, "state": "all", "from": "2026-03-10", "to": "2026-03-11"})).json()
    assert ev["total"] == 4 and ev["counts"]["history"] == 4
    x = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "all", "from": "2026-03-10", "to": "2026-03-11"})
    _, rows = read_xlsx(x.content)
    assert sum(1 for c in rows if c.get("E") in ("7413", "7414", "7500")) == 4


# ------------------------------------------------------------------ Excel
async def test_excel_note_and_data(users, anon, pool):
    plc = await _two_stations(pool)
    p = {"brand": "GWM", "from": "2026-03-10", "to": "2026-03-11", "all_hours": "true"}
    _, rows = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p)).content)
    assert not any(str(c.get("A", "")).startswith("Убрано из статистики") for c in rows)        # ничего не убрано: строки нет
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 1})
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "station", "station": "MB110"})
    r = await anon.get("/api/alarms/analytics.xlsx", params=p)
    z, rows = read_xlsx(r.content)
    assert rows[3]["A"] == "Убрано из статистики: 2 (аварий 1, станций 1)"
    j = await _day(anon, brand="GWM")
    assert rows[3]["A"] == _a().removed_note(j["removed"])
    tl = next(i for i, c in enumerate(rows) if c.get("A") == "Событий")
    assert tl > 3 and rows[tl + 1]["A"] == str(j["tiles"]["events"]) == "1"
    _, data = _table(rows, "Самые частые")
    assert [d["C"] for d in data] == ["7414"]
    _, data = _table(rows, "По станциям")
    assert [d["B"] for d in data] == ["MB100"]
    _, data = _table(rows, "Самые долгие случаи")
    assert len(data) == 1


@pytest.mark.skipif(not NODE, reason="нет node")
async def test_excel_note_matches_screen_text(users, anon, pool):
    plc = await _two_stations(pool)
    await users["t_master"].post(BASE, json={"plc_id": plc, "kind": "alarm", "bit_index": 2})
    _, rows = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params={"plc_id": plc, "from": "2026-03-10", "to": "2026-03-11"})).content)
    j = (await _get(anon, plc_id=plc)).json()
    assert rows[3]["A"] == node("u.anRemovedNote(%s)" % json.dumps(j["removed"]))


# ------------------------------------------------------------------ JS (node) и статика
@pytest.mark.skipif(not NODE, reason="нет node")
@pytest.mark.parametrize("user,ok", [(None, False), ({"role": "viewer"}, False), ({"role": "master"}, True), ({"role": "admin"}, True),
                                     ({"role": "maintenance"}, True), ({"role": "chief"}, True), ({"role": "area_head"}, True)])
def test_js_buttons_visible_by_role(user, ok):
    assert node(f"u.anCanExclude({json.dumps(user)})") is ok


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_removed_note_and_body():
    assert node("u.anRemovedNote({alarms:1,stations:2})") == "Убрано из статистики: 3 (аварий 1, станций 2)"
    assert node("u.anRemovedNote({alarms:0,stations:0})") == ""
    assert node("u.anExclusionBody('alarm',{plc_id:4,bit_index:12,station:'MB100'},' нет линии ')") == {"plc_id": 4, "kind": "alarm", "bit_index": 12, "reason": "нет линии"}
    assert node("u.anExclusionBody('station',{plc_id:4,station:'MB100'},'')") == {"plc_id": 4, "kind": "station", "station": "MB100"}
    line = node("u.anRemovedItem({plc:'GWM_MB',kind:'alarm',station:'MB100',code:'7413',text:'clamping timeout',reason:'не используется',"
                "created_name:'Исимов, Начальник участка',created_at:'2026-03-10T07:05:00Z'},5)")
    assert line["what"] == "MB100 · 7413 · clamping timeout" and line["author"] == "Исимов, Начальник участка · 10.03 12:05" and line["reason"] == "не используется"
    st = node("u.anRemovedItem({plc:'GWM_MB',kind:'station',station:'MB100',created_name:'А, Мастер',created_at:'2026-03-10T07:05:00Z'},5)")
    assert st["what"] == "Станция MB100 целиком"


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_action_column_not_in_copy_or_sort():
    cols = node("Object.fromEntries(Object.entries(u.anTables()).map(([k,v])=>[k,v.cols.map(c=>c.k)]))")
    assert all("action" not in v and "exclude" not in v for v in cols.values())      # колонка кнопок не часть таблицы: ни в Excel, ни в копировании


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_request_helper_errors_and_refresh_order():
    code = r"""
const u=require(%s);
(async()=>{
  const calls=[];
  const ok=async(url,o)=>{calls.push([url,o.method,o.body]);return {ok:true,status:200,json:async()=>({created:true})}};
  const bad=async()=>({ok:false,status:403,json:async()=>({detail:'У вашей роли нет права менять настройки'})});
  const a=await u.exclusionRequest(ok,'POST','/api/alarms/exclusions',{plc_id:1,kind:'alarm',bit_index:3});
  let msg='';
  try{await u.exclusionRequest(bad,'DELETE','/api/alarms/exclusions/5');}catch(e){msg=e.message;}
  console.log(JSON.stringify({a,calls,msg}));
})();""" % json.dumps(str(STATIC / "alarms.js"))
    import subprocess
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    res = json.loads(out.stdout)
    assert res["a"] == {"created": True} and res["calls"][0][1] == "POST" and json.loads(res["calls"][0][2]) == {"plc_id": 1, "kind": "alarm", "bit_index": 3}
    assert res["msg"] == "У вашей роли нет права менять настройки"


def test_screen_static_checks():
    css = (STATIC / "alarms.css").read_text(encoding="utf-8")
    assert "Убрать эту аварию из статистики аналитики. Журнал событий не меняется. Вернуть можно в списке «Убрано из статистики»" in JS
    assert "Убрать станцию целиком из статистики: все её аварии перестанут учитываться в аналитике. Журнал событий не меняется" in JS
    assert ">Убрать</button>" in JS and "Убрать станцию</button>" in JS and "Вернуть</button>" in JS
    assert "/api/alarms/exclusions" in JS and "anCanExclude(" in JS
    assert re.search(r"await exclusionRequest\([^;]*\);\s*(?:closeDlg\(\);\s*)?await refreshAn\(\)", JS)        # после действия аналитика перечитывается сразу
    assert 'id="al-an-removed"' in JS and re.search(r'id="al-an-removed"[^>]*title="[^"]{20,}"', JS)
    assert "attachColWidths(host.querySelector('table'), `alarms-an-${key}`)" in JS
    assert ".al-rm" in css and not re.search(r"#[0-9a-fA-F]{3,8}\b", css.split("/* Аналитика")[1])
    assert "flex-wrap" in css.split(".al-rm-row")[1]
    for k in ("cases", "frequent", "longest", "stations"):
        assert k in JS
    assert "window.authState" in JS and "window.onAuthChanged" in JS
    # кнопки действия не попадают в данные, которые копируются и сортируются
    assert "data-act-col" in JS


def test_reference_changelog_and_text():
    ref = (STATIC / "reference.html").read_text(encoding="utf-8")
    assert "Убрано из статистики" in ref
    first = re.search(r'<ul id="changelog">\s*<li data-date="([\d-]+)">', ref)
    assert first and first.group(1) == "2026-10-14"
