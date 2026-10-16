"""Журнал аварий, шаг 3A: станция и устройство из текста, API журнала (/events, /filters) и Excel (/events.xlsx)."""
import io
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pytest

_n = 0
NSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _a():
    import alarms
    return alarms


# ------------------------------------------------------------------ разбор станции
@pytest.mark.parametrize("text,expected", [
    ("Device profinet （Equipment ID)3 lost UB10_HMI1", ("UB10", "HMI1")),
    ("Device profinet （Equipment ID)10 Error UB10_HMI1_IM1", ("UB10", "HMI1_IM1")),
    ("Device profinet （Equipment ID)2 lost UB_PLC", ("", "PLC")),
    ("Device profinet （Equipment ID)145 Lost VDP1_UB20_VFD2_Trans", ("UB20", "VDP1_VFD2_Trans")),
    ("Device profinet （Equipment ID)22 lost UB010JB1_IM1", ("UB10", "JB1_IM1")),
    ("Device profinet （Equipment ID)29 Lost MCP_EX2", ("", "MCP_EX2")),
    ("Device profinet （Equipment ID)47 Error", ("", "")),
    ("Warning:UB20 body-detail sensor is blocked", ("UB20", "")),
    ("Warning:MB100/left positioning slide/left side fixture body-detail sensor", ("MB100", "")),
    ("Fault:The vehicle model at the UB70 station does not match", ("UB70", "")),
    ("Warning: The MB110L-3D gun needs to be dressed", ("MB110", "")),
    ("MB100_W32L_C4.5 clamping timeout", ("MB100", "")),
    ("UB040 horizontal motor drive alarm", ("UB40", "")),
    ("Emergency Stop alarm", ("", "")),
    ("", ("", "")),
    (None, ("", "")),
])
def test_parse_station_device(text, expected):
    assert _a().parse_station_device(text) == expected


# ------------------------------------------------------------------ помощники
async def _plc(pool, name=None):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.1.2.3:4840', TRUE) RETURNING id", name or f"jr{_n}")


async def _cat(pool, plc, bit, text, code="", cls="Alarm", station="", device=""):
    await pool.execute(
        "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, code, text, class, is_placeholder, station, device, parsed) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE, $8, $9, TRUE)",
        plc, bit, bit // 16, bit % 16, code, text, cls, station, device)


async def _ev(pool, plc, bit, started, ended=None, at_start=False):
    return await pool.fetchval(
        "INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at, active_at_start) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        plc, bit, started, ended, at_start)


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


async def _setup(pool):
    """Три аварии словаря, пять событий: два активных, три снятых."""
    plc = await _plc(pool, "ЖурналПЛК")
    await _cat(pool, plc, 1, "UB20 body-detail sensor is blocked", "101", "Warning", "UB20", "")
    await _cat(pool, plc, 2, "Lost UB10_HMI1 link", "102", "Fault", "UB10", "HMI1")
    await _cat(pool, plc, 3, "MB100 clamp 50%_x timeout", "103", "Warning", "MB100", "")
    now = datetime.now(timezone.utc)
    await _ev(pool, plc, 1, now - timedelta(hours=2))                                        # активна, бит 1
    await _ev(pool, plc, 99, now - timedelta(minutes=5), at_start=True)                      # активна, нет в словаре
    await _ev(pool, plc, 2, _utc(2026, 3, 10, 1, 0), _utc(2026, 3, 10, 1, 10))               # 10 мин
    await _ev(pool, plc, 3, _utc(2026, 3, 10, 5, 0), _utc(2026, 3, 10, 7, 0, 30))            # 2 ч 30 с
    await _ev(pool, plc, 1, _utc(2026, 3, 12, 6, 0), _utc(2026, 3, 12, 6, 0, 5))             # 5 с
    return plc


async def _get(anon, plc, **kw):
    kw = {k: v for k, v in kw.items() if v is not None}
    r = await anon.get("/api/alarms/events", params={"plc_id": plc, **kw})
    return r


# ------------------------------------------------------------------ ensure и импорт
async def test_ensure_fills_station_for_existing_rows(app, pool):
    plc = await _plc(pool)
    await pool.execute(
        "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, text, is_placeholder) VALUES "
        "($1, 0, 0, 0, 'Device profinet （Equipment ID)3 lost UB10_HMI1', FALSE), ($1, 1, 0, 1, 'Fault:UB070 jam', FALSE), "
        "($1, 2, 0, 2, '', TRUE)", plc)
    await _a().ensure_alarms_schema(pool)
    rows = {r["bit_index"]: (r["station"], r["device"], r["parsed"]) for r in await pool.fetch("SELECT * FROM alarm_catalog WHERE plc_id = $1", plc)}
    assert rows[0] == ("UB10", "HMI1", True)
    assert rows[1] == ("UB70", "", True)
    assert rows[2] == ("", "", True)
    # повторный запуск ничего не пересчитывает
    await pool.execute("UPDATE alarm_catalog SET station = 'X' WHERE plc_id = $1 AND bit_index = 1", plc)
    await _a().ensure_alarms_schema(pool)
    assert await pool.fetchval("SELECT station FROM alarm_catalog WHERE plc_id = $1 AND bit_index = 1", plc) == "X"


async def test_import_fills_station(users, pool):
    from test_alarms_catalog import HEADER, make_xlsx
    plc = await _plc(pool)
    data = make_xlsx([HEADER, {"B": "a", "C": "7_Warning:MB110 gun needs dressing", "E": "Warning", "F": "t", "G": 4}])
    r = await users["t_admin"].post(f"/api/alarms/catalog/import?plc_id={plc}", content=data)
    assert r.status_code == 200, r.text
    row = await pool.fetchrow("SELECT station, device FROM alarm_catalog WHERE plc_id = $1 AND bit_index = 4", plc)
    assert (row["station"], row["device"]) == ("MB110", "")


# ------------------------------------------------------------------ события
async def test_events_without_login_default_active(anon, pool):
    plc = await _setup(pool)
    r = await _get(anon, plc)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["plc"] == {"id": plc, "name": "ЖурналПЛК", "ip": "10.1.2.3"}
    assert j["link_state"] in ("ok", "no_link", "stale", "unknown", "off")
    assert j["counts"] == {"active": 2, "history": 3}
    assert j["total"] == 2 and len(j["items"]) == 2
    assert j["offset_hours"] == _a().UTC_OFFSET_HOURS
    assert j["now"].endswith("Z")
    assert all(i["active"] and i["ended_at"] is None and i["source"] == "HMI" for i in j["items"])
    first = j["items"][0]   # по умолчанию новые сверху
    assert first["bit_index"] == 99 and first["text"] == "Бит 99 (нет в словаре)" and first["active_at_start"] is True
    assert 290 <= first["duration_sec"] <= 330
    second = j["items"][1]
    assert second["code"] == "101" and second["station"] == "UB20" and second["class"] == "Warning"
    assert 7190 <= second["duration_sec"] <= 7260


async def test_events_states(anon, pool):
    plc = await _setup(pool)
    assert (await _get(anon, plc, state="all")).json()["total"] == 5
    h = (await _get(anon, plc, state="history")).json()
    assert h["total"] == 3 and all(not i["active"] and i["ended_at"] for i in h["items"])
    by_bit = {i["bit_index"]: i for i in h["items"]}
    assert by_bit[2]["duration_sec"] == 600
    assert by_bit[3]["duration_sec"] == 7230 and by_bit[3]["duration_text"] == "2 ч 0 мин 30 с"
    assert by_bit[1]["duration_text"] == "5 с"
    assert by_bit[2]["started_at"] == "2026-03-10T01:00:00Z" and by_bit[2]["ended_at"] == "2026-03-10T01:10:00Z"


async def test_events_bad_state_and_period(anon, pool):
    plc = await _setup(pool)
    assert (await _get(anon, plc, state="bogus")).status_code == 422
    assert (await _get(anon, plc, state="history", **{"from": "2026-03-12", "to": "2026-03-10"})).status_code == 400
    assert (await _get(anon, plc, state="history", **{"from": "2026-01-01", "to": "2026-04-05"})).status_code == 400   # 95 дней
    assert (await _get(anon, plc, state="history", **{"from": "2026-01-01", "to": "2026-04-03"})).status_code == 200    # ровно 93
    assert (await _get(anon, plc, limit=0)).status_code == 422
    assert (await _get(anon, plc, limit=1001)).status_code == 422
    assert (await _get(anon, plc + 1000)).status_code == 404


async def test_events_period_uses_plant_days(anon, pool):
    plc = await _plc(pool)
    a = _a()
    off = a.UTC_OFFSET_HOURS
    d = date(2026, 3, 10)
    start = a.plant_day_start_utc(d)
    assert start == _utc(2026, 3, 10) - timedelta(hours=off)
    await _cat(pool, plc, 1, "UB20 a")
    await _ev(pool, plc, 1, start, start + timedelta(minutes=1))                                   # первая секунда суток: входит
    await _ev(pool, plc, 1, start + timedelta(hours=24) - timedelta(seconds=1), start + timedelta(hours=25))   # последняя секунда: входит
    await _ev(pool, plc, 1, start - timedelta(seconds=1), start + timedelta(minutes=5))             # накануне: не входит
    await _ev(pool, plc, 1, start + timedelta(hours=24), start + timedelta(hours=25))               # следующие сутки: не входит
    j = (await _get(anon, plc, state="history", **{"from": "2026-03-10", "to": "2026-03-10"})).json()
    assert j["total"] == 2 and j["counts"]["history"] == 2
    j2 = (await _get(anon, plc, state="history", **{"from": "2026-03-10", "to": "2026-03-11"})).json()
    assert j2["total"] == 3
    # у активных период не применяется
    await _ev(pool, plc, 2, _utc(2020, 1, 1))
    ja = (await _get(anon, plc, state="active", **{"from": "2026-03-10", "to": "2026-03-10"})).json()
    assert ja["total"] == 1 and ja["counts"]["active"] == 1


async def test_events_filters(anon, pool):
    plc = await _setup(pool)
    g = lambda **kw: _get(anon, plc, state="all", **kw)  # noqa: E731
    assert [i["bit_index"] for i in (await g(station="UB20")).json()["items"]] == [1, 1]
    assert (await g(station="UB20")).json()["total"] == 2
    assert (await g(device="HMI1")).json()["total"] == 1
    assert (await g(**{"class": "Warning"})).json()["total"] == 3
    assert (await g(**{"class": "Fault"})).json()["total"] == 1
    assert (await g(q="BODY-DETAIL")).json()["total"] == 2          # без учёта регистра
    assert (await g(q="103")).json()["total"] == 1                  # точный код
    assert (await g(q="10")).json()["total"] == 2                   # "10" есть в тексте (UB10, MB100), код "102" целиком не равен "10"
    assert (await g(q="50%_x")).json()["total"] == 1                # % и _ ищутся буквально
    assert (await g(q="%")).json()["total"] == 1
    assert (await g(q="5_")).json()["total"] == 0
    assert (await g(station="UB20", q="hmi")).json()["total"] == 0
    # фильтры действуют и на счётчики
    c = (await g(station="UB20")).json()["counts"]
    assert c == {"active": 1, "history": 1}


async def test_events_sort_and_paging(anon, pool):
    plc = await _setup(pool)
    asc = await _get(anon, plc, state="history", sort="started_at", dir="asc")
    assert [i["bit_index"] for i in asc.json()["items"]] == [2, 3, 1]
    desc = await _get(anon, plc, state="history", sort="started_at", dir="desc")
    assert [i["bit_index"] for i in desc.json()["items"]] == [1, 3, 2]
    dur = await _get(anon, plc, state="history", sort="duration", dir="desc")
    assert [i["bit_index"] for i in dur.json()["items"]] == [3, 2, 1]
    code = await _get(anon, plc, state="history", sort="code", dir="asc")
    assert [i["code"] for i in code.json()["items"]] == ["101", "102", "103"]
    st = await _get(anon, plc, state="history", sort="station", dir="asc")
    assert [i["station"] for i in st.json()["items"]] == ["MB100", "UB10", "UB20"]
    ended = await _get(anon, plc, state="all", sort="ended_at", dir="desc")
    assert [i["active"] for i in ended.json()["items"]][-2:] == [True, True]    # без даты снятия всегда в конце
    # длительность активных: самая долгая активная выше событий истории по убыванию
    d_all = (await _get(anon, plc, state="all", sort="duration", dir="desc")).json()["items"]
    assert d_all[0]["bit_index"] == 3 and d_all[1]["active"] and d_all[1]["bit_index"] == 1   # активная считается от now
    # страницы
    p1 = (await _get(anon, plc, state="all", sort="started_at", dir="asc", limit=2, offset=0)).json()
    p2 = (await _get(anon, plc, state="all", sort="started_at", dir="asc", limit=2, offset=2)).json()
    p3 = (await _get(anon, plc, state="all", sort="started_at", dir="asc", limit=2, offset=4)).json()
    assert p1["total"] == p2["total"] == p3["total"] == 5
    ids = [i["id"] for p in (p1, p2, p3) for i in p["items"]]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert (await _get(anon, plc, sort="bogus")).status_code == 422
    assert (await _get(anon, plc, dir="up")).status_code == 422


async def test_events_other_plc_not_mixed(anon, pool):
    plc = await _setup(pool)
    other = await _plc(pool)
    await _ev(pool, other, 1, datetime.now(timezone.utc))
    assert (await _get(anon, plc, state="all")).json()["total"] == 5
    assert (await _get(anon, other, state="all")).json()["total"] == 1


# ------------------------------------------------------------------ фильтры
async def test_filters_endpoint(anon, pool):
    plc = await _setup(pool)
    await pool.execute("INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, is_placeholder, parsed) VALUES ($1, 50, 3, 2, TRUE, TRUE)", plc)
    r = await anon.get("/api/alarms/filters", params={"plc_id": plc})
    assert r.status_code == 200
    assert r.json() == {"stations": ["MB100", "UB10", "UB20"], "devices": ["HMI1"], "classes": ["Fault", "Warning"], "configured": True}


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
            col = re.sub(r"\d", "", c.get("r"))
            if v is None:
                continue
            cells[col] = sst[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(cells)
    return z, rows


def _fname(r):
    cd = r.headers["content-disposition"]
    return unquote(cd.split("filename*=UTF-8''")[1])


async def test_excel_matches_api_and_names(anon, pool):
    plc = await _setup(pool)
    r = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "all", "from": "2026-03-10", "to": "2026-03-12", "src": "alarms"})
    assert r.status_code == 200, r.text
    assert _fname(r) == "Аварии_ЖурналПЛК_2026-03-10_2026-03-12.xlsx"
    z, rows = read_xlsx(r.content)
    assert "xl/charts/chart1.xml" not in z.namelist()
    assert "Журнал аварий" in z.read("xl/workbook.xml").decode("utf-8")
    api = (await _get(anon, plc, state="all", **{"from": "2026-03-10", "to": "2026-03-12"})).json()
    # плитки
    tiles = next(i for i, c in enumerate(rows) if c.get("A") == "Активные")
    assert rows[tiles + 1]["A"] == str(api["counts"]["active"]) and rows[tiles + 1]["B"] == str(api["counts"]["history"])
    head = next(c for c in rows if c.get("A") == "Появление")
    assert [head[k] for k in "ABCDEFGHIJK"] == [
        "Появление", "Снятие", "Станция", "Устройство", "Код", "Текст аварии", "Источник", "Класс", "Состояние", "Длительность", "Длительность, мин"]
    hi = rows.index(head)
    data_rows = [c for c in rows[hi + 1:] if c.get("G") == "HMI"]
    assert len(data_rows) == api["total"] == 5
    texts = sorted(c["F"] for c in data_rows)
    assert texts == sorted(i["text"] for i in api["items"])
    durs = sorted(c["J"] for c in data_rows)
    assert durs == sorted(i["duration_text"] for i in api["items"])
    assert any(c["I"] == "активна" for c in data_rows) and any(c["I"] == "снята" for c in data_rows)
    # плантовое время: 2026-03-10 01:00 UTC + смещение
    off = _a().UTC_OFFSET_HOURS
    want = (datetime(2026, 3, 10, 1, 0) + timedelta(hours=off) - datetime(1899, 12, 30)).total_seconds() / 86400
    assert any(abs(float(c["A"]) - want) < 1e-6 for c in data_rows if re.fullmatch(r"[\d.]+", c["A"]))
    # итоговая строка
    total = rows[-1]
    assert total["A"] == "Итого записей" and total["B"] == "5"


async def test_excel_single_day_name_and_filters(anon, pool):
    plc = await _setup(pool)
    r = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "history", "from": "2026-03-10", "to": "2026-03-10", "station": "UB10"})
    assert r.status_code == 200
    assert _fname(r) == "Аварии_ЖурналПЛК_2026-03-10.xlsx"
    _, rows = read_xlsx(r.content)
    assert len([c for c in rows if c.get("G") == "HMI"]) == 1
    # без периода: имя с сегодняшней плантовой датой
    r2 = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc})
    assert _fname(r2) == f"Аварии_ЖурналПЛК_{_a().plant_today().isoformat()}.xlsx"


async def test_excel_bad_params(anon, pool):
    plc = await _setup(pool)
    r = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "from": "2026-03-12", "to": "2026-03-10"})
    assert r.status_code == 400
    assert (await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "x"})).status_code == 422


async def test_excel_too_many_rows(anon, pool, monkeypatch):
    plc = await _setup(pool)
    monkeypatch.setattr(_a(), "MAX_EXCEL_ROWS", 3)
    r = await anon.get("/api/alarms/events.xlsx", params={"plc_id": plc, "state": "all"})
    assert r.status_code == 413
    assert "максимум 3" in r.json()["detail"]


def test_src_name_registered():
    from history import SRC_NAMES, download_name
    assert SRC_NAMES["alarms"] == "Аварии"
    assert download_name("alarms", "x.xlsx") == "Аварии_x.xlsx"


def test_format_duration():
    f = _a().format_duration
    assert f(94853) == "26 ч 20 мин 53 с"
    assert f(303) == "5 мин 3 с"
    assert f(12) == "12 с"
    assert f(0) == "0 с"
