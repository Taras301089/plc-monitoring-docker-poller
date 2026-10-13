"""Аналитика аварий: расчёт топов и длительностей, правило рабочего времени, фильтры линия/участок, период, Excel и его соответствие экрану."""
import io
import json
import re
import shutil
import subprocess
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "alarms.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
NSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_n = 0


def _a():
    import alarms
    return alarms


def node(expr):
    code = f"const u=require({json.dumps(str(STATIC / 'alarms.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def P(day: str, hh: int, mm: int = 0) -> datetime:
    """Время по часам завода (плантовое) -> UTC."""
    return _a().plant_day_start_utc(date.fromisoformat(day)) + timedelta(hours=hh, minutes=mm)


# ------------------------------------------------------------------ помощники БД
async def _plc(pool, name):
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.1.2.3:4840', TRUE) RETURNING id", name)


async def _cat(pool, plc, bit, text, code="", station=""):
    await pool.execute(
        "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, code, text, class, is_placeholder, station, device, parsed) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'Alarm', FALSE, $7, '', TRUE)", plc, bit, bit // 16, bit % 16, code, text, station)


async def _ev(pool, plc, bit, started, ended=None):
    return await pool.fetchval(
        "INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at) VALUES ($1, $2, $3, $4) RETURNING id", plc, bit, started, ended)


async def _screen(pool, plc, name="GWM", template="body_counter"):
    return await pool.fetchval("INSERT INTO kpi_screens (name, plc_id, template) VALUES ($1, $2, $3) RETURNING id", name, plc, template)


async def _hour(pool, scr, day, idx, start, end, fact):
    await pool.execute("INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, 10, $6)",
                       scr, date.fromisoformat(day), idx, start, end, fact)


async def _worktime_setup(pool):
    """GWM_UB с экраном KPI: 10.03 смена 08:00-10:00 (факт 5), 10:00-12:00 выключен в расписании (факт 3), 12:00-13:00 включён (факт 0);
    11.03 выпуска нет. Шесть аварий на разных участках времени."""
    plc = await _plc(pool, "GWM_UB")
    await _cat(pool, plc, 1, "UB20 sensor blocked", "101", "UB20")
    await _cat(pool, plc, 2, "UB30 motor overload", "102", "UB30")
    scr = await _screen(pool, plc)
    await _hour(pool, scr, "2026-03-10", 1, 480, 600, 5)
    await _hour(pool, scr, "2026-03-10", 2, 600, 720, 3)
    await _hour(pool, scr, "2026-03-10", 3, 720, 780, 0)
    await _hour(pool, scr, "2026-03-11", 1, 480, 600, 0)
    await pool.execute("INSERT INTO kpi_schedule (screen_id, idx, end_min, plan, active) VALUES ($1, 1, 600, 10, TRUE), ($1, 2, 720, 10, FALSE)", scr)
    await _ev(pool, plc, 1, P("2026-03-10", 8, 30), P("2026-03-10", 8, 40))      # в смене: 10 мин
    await _ev(pool, plc, 2, P("2026-03-10", 9, 50), P("2026-03-10", 10, 20))     # начата в смене, уходит в выключенный интервал: 10 из 30 мин
    await _ev(pool, plc, 1, P("2026-03-10", 10, 30), P("2026-03-10", 10, 40))    # выключенный интервал
    await _ev(pool, plc, 1, P("2026-03-10", 7, 0), P("2026-03-10", 7, 10))       # до смены
    await _ev(pool, plc, 2, P("2026-03-11", 8, 30), P("2026-03-11", 8, 35))      # день без выпуска
    await _ev(pool, plc, 1, P("2026-03-10", 12, 10), P("2026-03-10", 12, 20))    # включённый интервал дня с выпуском (факт 0 в интервале)
    return plc


async def _get(client, **params):
    params.setdefault("from", "2026-03-10")
    params.setdefault("to", "2026-03-11")
    return await client.get("/api/alarms/analytics", params=params)


# ------------------------------------------------------------------ бренд и участок
@pytest.mark.parametrize("name,brand,area", [
    ("GWM_UB", "GWM", "UB"), ("GWM_BS", "GWM", "BS"), ("Chery_MB", "Chery", "MB"), ("Changan", "Changan", ""),
    ("AE_WBS_1", "AE", "WBS 1"), ("AE_WBS_2", "AE", "WBS 2"), ("", "", ""), (None, "", ""),
])
def test_brand_area(name, brand, area):
    assert _a().plc_brand(name) == brand and _a().plc_area(name) == area


# ------------------------------------------------------------------ расчёт (чистая функция)
def _event(plc_id, bit, start, end, plc="X", text="авария", station="UB20", code="1"):
    return {"plc_id": plc_id, "plc": plc, "bit_index": bit, "started_at": start, "ended_at": end, "code": code,
            "text": text, "station": station, "in_catalog": True}


def test_merge_intervals():
    t = lambda h: datetime(2026, 3, 10, h, tzinfo=timezone.utc)  # noqa: E731
    assert _a().merge_intervals([(t(8), t(10)), (t(9), t(11)), (t(11), t(12)), (t(14), t(15)), (t(5), t(5))]) == [(t(8), t(12)), (t(14), t(15))]


def test_build_analytics_clips_and_excludes():
    a = _a()
    t = lambda h, m=0: datetime(2026, 3, 10, h, m, tzinfo=timezone.utc)  # noqa: E731
    work = {1: [(t(10), t(12))], 2: None}
    now = t(20)
    evs = [
        _event(1, 1, t(9), t(9, 30)),            # до смены: не считается
        _event(1, 2, t(11, 30), t(12, 30)),      # пересекает конец смены: 30 мин
        _event(1, 3, t(10), t(10, 10)),          # начало на границе включается: 10 мин
        _event(1, 4, t(12), t(12, 5)),           # начало на конце интервала: не считается
        _event(1, 5, t(9, 50), t(10, 20)),       # начата до смены: не считается, хоть и заходит в смену
        _event(2, 1, t(3), t(4)),                # ПЛК без экрана: рабочее всё время, 60 мин
        _event(1, 6, t(11), None),               # активная: до «сейчас», обрезана концом смены: 60 мин
    ]
    r = a.build_analytics(evs, work, now, include_all=False)
    assert r["tiles"] == {"events": 4, "total_sec": (30 + 10 + 60 + 60) * 60, "distinct": 4}
    assert r["excluded"] == 3
    full = a.build_analytics(evs, work, now, include_all=True)
    assert full["excluded"] == 0 and full["tiles"]["events"] == 7
    assert full["tiles"]["total_sec"] == (30 + 60 + 10 + 5 + 30 + 60 + 9 * 60) * 60


def test_build_analytics_tops_and_shares():
    a = _a()
    t = lambda h, m=0: datetime(2026, 3, 10, h, m, tzinfo=timezone.utc)  # noqa: E731
    evs = [_event(1, 1, t(1), t(1, 10), text="A", station="UB20"), _event(1, 1, t(2), t(2, 20), text="A", station="UB20"),
           _event(1, 1, t(3), t(3, 5), text="A", station="UB20"), _event(1, 2, t(4), t(5, 40), text="B", station="MB10"),
           _event(1, 3, t(6), t(6, 10), text="C", station="MB10")]
    r = a.build_analytics(evs, None, t(23), include_all=True)
    assert [x["text"] for x in r["frequent"]][0] == "A"
    f = r["frequent"][0]
    assert (f["count"], f["total_sec"], f["max_sec"], f["avg_sec"]) == (3, 35 * 60, 20 * 60, 700.0)
    assert f["share_count"] == pytest.approx(3 / 5, abs=1e-5) and f["share_time"] == pytest.approx(35 / 145, abs=1e-5)
    assert [x["text"] for x in r["longest"]][0] == "B"
    assert r["cases"][0]["text"] == "B" and r["cases"][0]["duration_sec"] == 100 * 60
    st = {x["station"]: x for x in r["stations"]}
    assert st["MB10"]["count"] == 2 and st["MB10"]["total_sec"] == 110 * 60
    assert sum(x["count"] for x in r["stations"]) == r["tiles"]["events"] == 5
    assert sum(x["total_sec"] for x in r["stations"]) == r["tiles"]["total_sec"] == 145 * 60
    assert sum(x["share_time"] for x in r["stations"]) == pytest.approx(1.0, abs=1e-4)
    cnt = r["pareto"]["count"]
    assert [p["value"] for p in cnt] == [3, 1, 1] and cnt[-1]["cum"] == 1.0 and cnt[0]["cum"] == pytest.approx(0.6)
    tm = r["pareto"]["time"]
    assert tm[0]["label"] == "B" and tm[0]["value"] == 100.0 and tm[-1]["cum"] == 1.0
    assert len(r["days"]) == 1 and r["days"][0]["count"] == 5


def test_build_analytics_empty():
    r = _a().build_analytics([], None, datetime.now(timezone.utc), include_all=False)
    assert r["tiles"] == {"events": 0, "total_sec": 0, "distinct": 0}
    assert r["frequent"] == [] and r["pareto"] == {"count": [], "time": []} and r["days"] == []


def test_unknown_bit_and_station_dash():
    a = _a()
    ev = {"plc_id": 1, "plc": "X", "bit_index": 77, "started_at": datetime(2026, 3, 10, tzinfo=timezone.utc),
          "ended_at": datetime(2026, 3, 10, 0, 1, tzinfo=timezone.utc), "code": "", "text": "", "station": "", "in_catalog": False}
    r = a.build_analytics([ev], None, datetime.now(timezone.utc), True)
    row = r["frequent"][0]
    assert row["text"] == "Бит 77 (нет в словаре)" and row["station"] == "—" and row["code"] == "—"


# ------------------------------------------------------------------ API: рабочее время
async def test_worktime_default_and_toggle(anon, pool):
    await _worktime_setup(pool)
    r = await _get(anon, brand="GWM")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["tiles"] == {"events": 3, "total_sec": 1800, "distinct": 2}
    assert j["excluded"] == 3 and j["include_all"] is False
    by = {x["code"]: x for x in j["frequent"]}
    assert by["101"]["count"] == 2 and by["101"]["total_sec"] == 1200 and by["102"]["count"] == 1 and by["102"]["total_sec"] == 600
    assert [d["date"] for d in j["days"]] == ["2026-03-10"]
    allj = (await _get(anon, brand="GWM", all_hours="true")).json()
    assert allj["tiles"] == {"events": 6, "total_sec": 4500, "distinct": 2}
    assert allj["excluded"] == 0 and allj["include_all"] is True
    assert [(d["date"], d["count"]) for d in allj["days"]] == [("2026-03-10", 5), ("2026-03-11", 1)]


async def test_plc_without_screen_counts_everything(anon, pool):
    plc = await _plc(pool, "Changan")
    await _cat(pool, plc, 1, "Door open", "1", "UB10")
    await _ev(pool, plc, 1, P("2026-03-10", 3, 0), P("2026-03-10", 3, 30))
    j = (await _get(anon, brand="Changan")).json()
    assert j["tiles"] == {"events": 1, "total_sec": 1800, "distinct": 1} and j["excluded"] == 0


async def test_union_of_several_screens(anon, pool):
    plc = await _plc(pool, "GWM_MB")
    await _cat(pool, plc, 1, "Door open", "1", "MB10")
    s1, s2 = await _screen(pool, plc, "GWM MB1"), await _screen(pool, plc, "GWM MB2")
    await _hour(pool, s1, "2026-03-10", 1, 480, 540, 4)
    await _hour(pool, s2, "2026-03-10", 1, 600, 660, 0)       # у второго экрана выпуска нет, но у ПЛК день рабочий: интервал входит
    await _ev(pool, plc, 1, P("2026-03-10", 8, 10), P("2026-03-10", 8, 20))
    await _ev(pool, plc, 1, P("2026-03-10", 10, 10), P("2026-03-10", 10, 20))
    await _ev(pool, plc, 1, P("2026-03-10", 9, 30), P("2026-03-10", 9, 40))   # между интервалами экранов
    j = (await _get(anon, plc_id=plc)).json()
    assert j["tiles"]["events"] == 2 and j["excluded"] == 1


async def test_interval_after_midnight(anon, pool):
    plc = await _plc(pool, "GWM_BS")
    await _cat(pool, plc, 1, "Door open", "1", "")
    scr = await _screen(pool, plc)
    await _hour(pool, scr, "2026-03-10", 1, 1380, 1500, 7)    # 23:00-01:00 следующих суток
    await _ev(pool, plc, 1, P("2026-03-11", 0, 30), P("2026-03-11", 0, 50))
    j = (await _get(anon, plc_id=plc)).json()
    assert j["tiles"]["events"] == 1 and j["tiles"]["total_sec"] == 1200


async def test_active_alarm_counts_until_now(anon, pool):
    plc = await _plc(pool, "Changan")
    await _cat(pool, plc, 1, "Door open", "1", "UB10")
    await _ev(pool, plc, 1, datetime.now(timezone.utc) - timedelta(minutes=10), None)
    t = _a().plant_today()
    j = (await _get(anon, plc_id=plc, **{"from": (t - timedelta(days=1)).isoformat(), "to": t.isoformat()})).json()
    assert j["tiles"]["events"] == 1 and 590 <= j["tiles"]["total_sec"] <= 700


# ------------------------------------------------------------------ API: фильтры линия / участок
async def _lines(pool):
    ids = {}
    for name in ["GWM_UB", "GWM_MB", "GWM_BS", "Chery_UB", "Chery_MB", "Changan", "AE_WBS_1", "AE_WBS_2"]:
        ids[name] = await _plc(pool, name)
        if name == "GWM_BS":
            continue            # у этого ПЛК нет ни словаря, ни событий
        await _cat(pool, ids[name], 1, f"alarm of {name}", "1", "UB10")
        await _ev(pool, ids[name], 1, P("2026-03-10", 9, 0), P("2026-03-10", 9, 10))
    return ids


async def test_filters_all_brand_plc(anon, pool):
    ids = await _lines(pool)
    base = dict(all_hours="true")
    assert (await _get(anon, **base)).json()["tiles"]["events"] == 7
    j = (await _get(anon, brand="GWM", **base)).json()
    assert j["tiles"]["events"] == 2 and [p["name"] for p in j["scope"]["plcs"]] == ["GWM_UB", "GWM_MB", "GWM_BS"] and j["scope"]["label"] == "GWM"
    assert (await _get(anon, brand="gwm", **base)).json()["tiles"]["events"] == 2
    assert (await _get(anon, brand="AE", **base)).json()["tiles"]["events"] == 2
    assert (await _get(anon, brand="Changan", **base)).json()["tiles"]["events"] == 1
    assert (await _get(anon, brand="Chery", **base)).json()["tiles"]["events"] == 2
    j = (await _get(anon, plc_id=ids["AE_WBS_2"], **base)).json()
    assert j["tiles"]["events"] == 1 and j["scope"]["label"] == "AE_WBS_2" and j["frequent"][0]["plc"] == "AE_WBS_2"
    # участок важнее бренда
    j = (await _get(anon, brand="GWM", plc_id=ids["GWM_MB"], **base)).json()
    assert j["tiles"]["events"] == 1 and j["frequent"][0]["plc"] == "GWM_MB"
    assert len({x["plc"] for x in (await _get(anon, **base)).json()["stations"]}) == 7


async def test_no_data_for_plc_without_catalog(anon, pool):
    ids = await _lines(pool)
    j = (await _get(anon, plc_id=ids["GWM_BS"])).json()
    assert j["scope"]["plcs_with_data"] == 0 and j["tiles"] == {"events": 0, "total_sec": 0, "distinct": 0}
    assert j["frequent"] == [] and j["pareto"] == {"count": [], "time": []}
    j = (await _get(anon, brand="Nonexistent")).json()
    assert j["scope"]["plcs"] == [] and j["scope"]["plcs_with_data"] == 0 and j["tiles"]["events"] == 0
    j = (await _get(anon, brand="GWM")).json()
    assert j["scope"]["plcs_with_data"] == 2


# ------------------------------------------------------------------ API: период и права
async def test_period_rules(anon, pool):
    await _worktime_setup(pool)
    r = await _get(anon, **{"from": "2026-03-11", "to": "2026-03-10"})
    assert r.status_code == 400 and "раньше" in r.json()["detail"]
    r = await _get(anon, **{"from": "2026-01-01", "to": "2026-06-01"})
    assert r.status_code == 400 and "93" in r.json()["detail"]
    assert (await anon.get("/api/alarms/analytics", params={"plc_id": 999999})).status_code == 404
    one = (await anon.get("/api/alarms/analytics")).json()
    t = _a().plant_today().isoformat()
    assert one["from"] == one["to"] == t
    # событие считается по плантовому дню его начала: 10.03 в 08:30 не попадает в период только 11.03
    day = (await _get(anon, brand="GWM", all_hours="true", **{"from": "2026-03-11", "to": "2026-03-11"})).json()
    assert day["tiles"]["events"] == 1
    assert len(day["days"]) == 1


async def test_view_permissions_like_journal(anon, users, pool):
    await _worktime_setup(pool)
    assert (await _get(anon, brand="GWM")).status_code == 200            # журнал аварий читается без входа, аналитика так же
    assert (await _get(users["t_viewer"], brand="GWM")).status_code == 200
    assert (await anon.get("/api/alarms/analytics.xlsx", params={"from": "2026-03-10", "to": "2026-03-10"})).status_code == 200


# ------------------------------------------------------------------ Excel
def read_xlsx(data: bytes):
    z = zipfile.ZipFile(io.BytesIO(data))
    sst = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NSX + "si"):
            sst.append("".join(t.text or "" for t in si.iter(NSX + "t")))
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(NSX + "row"):
        cells = {}
        for c in row.findall(NSX + "c"):
            v = c.find(NSX + "v")
            if v is None:
                continue
            cells[re.sub(r"\d", "", c.get("r"))] = sst[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(cells)
    return z, rows


def _fname(r):
    return unquote(r.headers["content-disposition"].split("filename*=UTF-8''")[1])


def _table(rows, title):
    """Строки таблицы по названию: (заголовки, строки данных до пустой или итоговой строки)."""
    i = next(k for k, c in enumerate(rows) if c.get("A") == title and len(c) == 1)
    head = rows[i + 1]
    data = []
    for c in rows[i + 2:]:
        if len(c) == 1 or c.get("A") == "Итого":
            break
        data.append(c)
    return head, data


COLS = "ABCDEFGHI"


async def test_excel_name_sheet_and_tiles(anon, pool):
    await _worktime_setup(pool)
    r = await anon.get("/api/alarms/analytics.xlsx", params={"brand": "GWM", "from": "2026-03-10", "to": "2026-03-11", "src": "alarms"})
    assert r.status_code == 200, r.text
    assert _fname(r) == "Аварии_GWM_2026-03-10_2026-03-11.xlsx"
    z, rows = read_xlsx(r.content)
    wb = z.read("xl/workbook.xml").decode("utf-8")
    assert 'name="Аналитика"' in wb and re.search(r'name="График"[^>]*state="hidden"', wb)
    assert rows[0]["A"] == "Аналитика аварий: GWM" and rows[1]["A"] == "Период: 10.03.2026 – 11.03.2026"
    j = (await _get(anon, brand="GWM")).json()
    assert rows[2]["A"] == f"Учитываются аварии, начавшиеся в рабочее время линии. Не учтено вне смены и в дни без выпуска: {j['excluded']}"
    tl = next(i for i, c in enumerate(rows) if c.get("A") == "Событий")
    assert [rows[tl][k] for k in "ABC"] == ["Событий", "Суммарное время аварий", "Разных аварий"]
    assert rows[tl + 1]["A"] == "3" and rows[tl + 1]["B"] == "30 мин" and rows[tl + 1]["C"] == "2"


async def test_excel_toggle_note_and_single_day_name(anon, pool):
    await _worktime_setup(pool)
    r = await anon.get("/api/alarms/analytics.xlsx", params={"plc_id": (await pool.fetchval("SELECT id FROM plcs")), "from": "2026-03-10", "to": "2026-03-10", "all_hours": "true"})
    assert _fname(r) == "Аварии_GWM_UB_2026-03-10.xlsx"
    _, rows = read_xlsx(r.content)
    assert rows[2]["A"] == "Учитываются все аварии, в том числе вне смены и в дни без выпуска"
    assert rows[0]["A"] == "Аналитика аварий: GWM_UB"
    assert not any(c.get("A") == "По дням" for c in rows)       # один день: разбивки по дням нет (как на экране)
    r = await anon.get("/api/alarms/analytics.xlsx", params={"from": "2026-03-10", "to": "2026-03-10"})
    assert _fname(r) == "Аварии_все_2026-03-10.xlsx"
    assert read_xlsx(r.content)[1][0]["A"] == "Аналитика аварий: все линии"


async def test_excel_matches_api(anon, pool):
    await _worktime_setup(pool)
    p = {"brand": "GWM", "from": "2026-03-10", "to": "2026-03-11", "all_hours": "true"}
    z, rows = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p)).content)
    j = (await _get(anon, brand="GWM", all_hours="true")).json()
    head, data = _table(rows, "Самые частые")
    assert [head[k] for k in COLS] == _a().FREQ_HEADERS + [_a().SHARE_COUNT]
    assert [d["D"] for d in data] == [x["text"] for x in j["frequent"]]
    assert [int(d["E"]) for d in data] == [x["count"] for x in j["frequent"]]
    assert [round(float(d["F"]) * 60) for d in data] == [x["total_sec"] for x in j["frequent"]]
    _, data = _table(rows, "Самые длительные")
    assert [d["D"] for d in data] == [x["text"] for x in j["longest"]]
    head, data = _table(rows, "Самые долгие случаи")
    assert [head[k] for k in "ABCDEF"] == _a().CASE_HEADERS and len(data) == 6
    head, data = _table(rows, "По станциям")
    assert [head[k] for k in "ABCDEF"] == _a().STATION_HEADERS
    assert sum(int(d["C"]) for d in data) == j["tiles"]["events"] == 6
    i = next(k for k, c in enumerate(rows) if c.get("A") == "По станциям")
    total = next(c for c in rows[i:] if c.get("A") == "Итого")
    assert int(total["C"]) == 6 and round(float(total["D"]) * 60) == j["tiles"]["total_sec"] == 4500
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert len(re.findall(r"<f>SUM\(", sheet)) >= 5
    head, data = _table(rows, "По дням")
    assert [head[k] for k in "ABCD"] == _a().DAY_HEADERS and [int(d["B"]) for d in data] == [5, 1]
    i = next(k for k, c in enumerate(rows) if c.get("A") == "По дням")
    total = next(c for c in rows[i:] if c.get("A") == "Итого")
    assert int(total["B"]) == 6 and round(float(total["C"]) * 60) == 4500


async def test_excel_pareto_chart_colors_and_metric(anon, pool):
    await _worktime_setup(pool)
    p = {"brand": "GWM", "from": "2026-03-10", "to": "2026-03-11", "all_hours": "true"}
    z, _ = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p)).content)
    charts = [n for n in z.namelist() if n.startswith("xl/charts/chart")]
    assert len(charts) == 1
    xml = z.read(charts[0]).decode("utf-8")
    assert "Парето аварий (топ-10), по числу появлений" in xml and "Накопленная доля" in xml
    assert "2F80ED" in xml and "27AE60" in xml            # столбцы синие, линия зелёная
    z, _ = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params={**p, "metric": "time"})).content)
    assert "Парето аварий (топ-10), по времени" in z.read("xl/charts/chart1.xml").decode("utf-8")
    assert (await anon.get("/api/alarms/analytics.xlsx", params={**p, "metric": "bad"})).status_code == 422


async def test_excel_without_data_is_valid(anon, pool):
    plc = await _plc(pool, "GWM_BS")
    r = await anon.get("/api/alarms/analytics.xlsx", params={"plc_id": plc, "from": "2026-03-10", "to": "2026-03-12"})
    assert r.status_code == 200
    z, rows = read_xlsx(r.content)
    assert not [n for n in z.namelist() if n.startswith("xl/charts/")]
    assert any(c.get("A") == "Нет аварий за выбранный период" for c in rows)


@pytest.mark.skipif(not NODE, reason="нет node")
async def test_excel_matches_screen_definition(anon, pool):
    """Плитки, график и таблицы экрана (alarms.js) совпадают с файлом: названия, колонки, текст примечания."""
    await _worktime_setup(pool)
    p = {"brand": "GWM", "from": "2026-03-10", "to": "2026-03-11"}
    z, rows = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p)).content)
    j = (await _get(anon, brand="GWM")).json()
    screen = node("{tiles:u.AN_TILES,pareto:u.AN_PARETO_TITLE,tables:Object.fromEntries(Object.entries(u.anTables()).map(([k,v])=>[k,[v.title,v.cols.map(c=>c.t)]])),note:u.anNote(false,%d)}" % j["excluded"])
    tl = next(i for i, c in enumerate(rows) if c.get("A") == "Событий")
    assert [rows[tl][k] for k in "ABC"] == screen["tiles"]
    assert rows[2]["A"] == screen["note"]
    assert screen["pareto"] in z.read("xl/charts/chart1.xml").decode("utf-8")
    assert [k for k in screen["tables"]] == ["frequent", "longest", "cases", "stations", "days"]
    for key, (title, cols) in screen["tables"].items():
        head, _ = _table(rows, title)
        assert [head[COLS[i]] for i in range(len(cols))] == cols, key
    assert screen["pareto"] == _a().PARETO_TITLE and screen["tiles"] == _a().TILE_NAMES
    assert {k: v[0] for k, v in screen["tables"].items()} == _a().TABLE_TITLES


# ------------------------------------------------------------------ JS (node) и статика
@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_brand_area_menus():
    plcs = [{"id": 1, "name": "GWM_UB"}, {"id": 2, "name": "GWM_MB"}, {"id": 3, "name": "GWM_BS"}, {"id": 4, "name": "Chery_UB"},
            {"id": 5, "name": "Changan"}, {"id": 6, "name": "AE_WBS_1"}, {"id": 7, "name": "AE_WBS_2"}]
    J = json.dumps(plcs)
    assert node(f"u.anBrands({J})") == ["GWM", "Chery", "Changan", "AE"]
    assert node(f"u.anAreas({J}, 'GWM')") == [{"id": "1", "name": "UB"}, {"id": "2", "name": "MB"}, {"id": "3", "name": "BS"}]
    assert node(f"u.anAreas({J}, 'AE')") == [{"id": "6", "name": "WBS 1"}, {"id": "7", "name": "WBS 2"}]
    assert node(f"u.anAreas({J}, 'Changan')") == []
    assert node("u.plcArea('AE_WBS_2')") == "WBS 2"


@pytest.mark.skipif(not NODE, reason="нет node")
@pytest.mark.parametrize("sec", [0, 5, 60, 1800, 3600, 3661, 94853])
def test_tile_duration_same_as_screen(sec):
    """Время в плитке Excel записано так же, как на экране (fmtDuration)."""
    assert _a().tile_duration(sec) == node(f"u.fmtDuration({sec})")


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_query_and_url():
    q = node("u.anQueryOf({from:'2026-03-01',to:'2026-03-10',anBrand:'GWM',anPlc:'',anAll:false})")
    assert q == {"from": "2026-03-01", "to": "2026-03-10", "brand": "GWM", "plc_id": "", "all_hours": ""}
    q = node("u.anQueryOf({from:'a',to:'b',anBrand:'GWM',anPlc:'5',anAll:true})")
    assert q["brand"] == "" and q["plc_id"] == "5" and q["all_hours"] == "true"
    url = node("u.buildUrl('/api/alarms/analytics.xlsx', Object.assign(u.anQueryOf({from:'2026-03-01',to:'2026-03-10',anBrand:'AE',anPlc:'',anAll:true}),{metric:'time',src:'alarms'}))")
    assert url == "/api/alarms/analytics.xlsx?brand=AE&from=2026-03-01&to=2026-03-10&metric=time&src=alarms&all_hours=true" or (
        url.startswith("/api/alarms/analytics.xlsx?") and all(x in url for x in ["from=2026-03-01", "to=2026-03-10", "brand=AE", "all_hours=true", "metric=time", "src=alarms"]))


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_sort_filter_totals():
    rows = [{"plc": "B", "station": "UB20", "count": 2, "total_sec": 600, "avg_sec": 300, "share_time": 0.5},
            {"plc": "A", "station": "UB10", "count": 10, "total_sec": 300, "avg_sec": 30, "share_time": 0.25},
            {"plc": "A", "station": "UB30", "count": 2, "total_sec": 300, "avg_sec": 150, "share_time": 0.25}]
    R = json.dumps(rows)
    cols = "u.anTables().stations.cols"
    # числа сортируются как числа, не как текст (10 больше 2)
    assert [r["station"] for r in node(f"u.sortRows({R}, {cols}, [{{k:'count',dir:'desc'}}], {{off:5}})")] == ["UB10", "UB20", "UB30"]
    assert [r["station"] for r in node(f"u.sortRows({R}, {cols}, [{{k:'plc',dir:'asc'}},{{k:'count',dir:'desc'}}], {{off:5}})")] == ["UB10", "UB30", "UB20"]
    assert [r["station"] for r in node(f"u.filterRows({R}, {cols}, {{plc:['A']}}, {{off:5}})")] == ["UB10", "UB30"]
    assert len(node(f"u.filterRows({R}, {cols}, {{plc:[]}}, {{off:5}})")) == 3
    assert node(f"u.nextSorts([], 'count', false)") == [{"k": "count", "dir": "asc"}]
    assert node("u.nextSorts([{k:'count',dir:'asc'}], 'count', false)") == [{"k": "count", "dir": "desc"}]
    assert node("u.nextSorts([{k:'count',dir:'desc'}], 'count', false)") == []
    assert node("u.nextSorts([{k:'count',dir:'desc'}], 'plc', true)") == [{"k": "count", "dir": "desc"}, {"k": "plc", "dir": "asc"}]
    # итоговая строка: то же, что SUM в Excel
    assert node(f"u.anTables().stations.totals({R})") == ["Итого", "", "14", "20,0", "1,4", "100,0"]
    assert node("u.anTables().days.totals([{count:2,total_sec:120},{count:1,total_sec:60}])") == ["Итого", "3", "3,0", "1,0"]
    assert node("u.anTables().days.cols[0].text({date:'2026-03-05'})") == "05.03.2026"


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_pareto_svg():
    rows = [{"label": "A", "plc": "GWM_UB", "station": "UB20", "value": 8, "total_sec": 480, "share": .5, "cum": .5},
            {"label": "B & <b>", "plc": "GWM_UB", "station": "—", "value": 8, "total_sec": 480, "share": .5, "cum": 1}]
    svg = node(f"u.paretoSvg({json.dumps(rows)}, 'count', 900)")
    assert svg.count('class="al-bar"') == 2 and 'class="al-cum"' in svg and svg.count('class="al-dot"') == 2
    assert "B &amp; &lt;b&gt;" in svg and "<b>" not in svg
    assert node("u.paretoSvg([], 'count', 900)") == ""
    assert "fill:" not in svg and "#" not in svg          # цвета только через CSS-переменные


def test_screen_static_checks():
    assert "/static/alarms.js" in INDEX and "/static/alarms.css" in INDEX         # подключение не менялось
    assert 'id="al-t-an"' in JS and "setMode('analytics')" in JS and "refreshAn" in JS
    assert "/api/alarms/analytics" in JS and "analytics.xlsx" in JS
    assert "AbortSignal.timeout(6000)" in JS
    assert "attachColWidths(host.querySelector('table'), `alarms-an-${key}`)" in JS
    assert "window.xlgrid.toTsv" in JS
    assert "Учитывать аварии вне смены и в дни без выпуска" in JS
    assert re.search(r'<input type="checkbox" id="al-an-all" title="[^"]{20,}"', JS)
    for el in ("al-an-brand", "al-an-area", "al-an-reset", "al-an-xl"):
        assert re.search(rf'id="{el}"[^>]*title="[^"]{{15,}}"', JS), el
    assert re.search(r'data-m="count" title="[^"]{15,}"', JS) and re.search(r'data-m="time" title="[^"]{15,}"', JS)
    assert "Нет данных: для выбранных ПЛК нет словаря аварий и событий" in JS
    css = (STATIC / "alarms.css").read_text(encoding="utf-8")
    assert ".al-bar { fill: var(--accent)" in css and ".al-cum { fill: none; stroke: var(--ok)" in css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.split("/* Аналитика")[1])


def test_reference_mentions_alarm_analytics():
    ref = (STATIC / "reference.html").read_text(encoding="utf-8")
    assert "режим «Аналитика»" in ref and "analytics.xlsx" in ref and "аналитика аварий (топы" in ref
