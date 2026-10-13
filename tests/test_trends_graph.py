"""Вкладка «Тренды», раздел «Графики переменных»: сервер (выбор сырых и агрегированных данных, корзины, проверка параметров,
права, индекс) и Excel (лист, график, названия)."""
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

OFF = 5
TZ = timezone(timedelta(hours=OFF))
T0 = datetime(2026, 10, 8, 10, 0, 0, tzinfo=TZ)
_n = 0


def iso(dt):
    return dt.isoformat()


async def _plc(pool, name=None, order=None):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active, sort_order) VALUES ($1, 'opc.tcp://10.0.0.1:4840', TRUE, $2) RETURNING id",
        name or f"gp{_n}", order)


async def _tag(pool, plc, name="Voltage L1-N", archived=True, ttype="ANALOG", deadband=None, lo=None, hi=None):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plc_tags (plc_id, name, node_id, tag_type, is_archived, deadband, limit_low, limit_high) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id", plc, name, f"ns=3;s=g{_n}", ttype, archived, deadband, lo, hi)


async def _put(pool, plc, tag, rows):
    await pool.executemany("INSERT INTO telemetry (ts, plc_id, tag_id, value) VALUES ($1, $2, $3, $4)", [(t, plc, tag, v) for t, v in rows])


async def _get(c, tags, f, t, buckets=None):
    q = f"/api/trends/series?from={iso(f).replace('+', '%2B')}&to={iso(t).replace('+', '%2B')}&tags={tags}"
    if buckets:
        q += f"&buckets={buckets}"
    return await c.get(q)


# ---------------------------------------------------------------- чистые функции
def test_parse_tag_ids_and_limits():
    from fastapi import HTTPException
    from trends import MAX_TAGS, parse_tag_ids
    assert parse_tag_ids("8, 9,10") == [8, 9, 10]
    assert parse_tag_ids("8,8,9") == [8, 9]
    assert MAX_TAGS == 8
    parse_tag_ids(",".join(str(i) for i in range(1, 9)))
    for bad in ("", " , ", "a", "1,x", "0", "-3", ",".join(str(i) for i in range(1, 10))):
        with pytest.raises(HTTPException) as e:
            parse_tag_ids(bad)
        assert e.value.status_code == 422, bad


def test_check_window():
    from fastapi import HTTPException
    from trends import check_window
    f, t = check_window(T0, T0 + timedelta(days=93))
    assert t - f == timedelta(days=93)
    for a, b in ((T0, T0), (T0 + timedelta(hours=1), T0), (T0, T0 + timedelta(days=93, seconds=1))):
        with pytest.raises(HTTPException) as e:
            check_window(a, b)
        assert e.value.status_code == 422
    naive = check_window(datetime(2026, 10, 8, 10), datetime(2026, 10, 8, 11))   # без пояса: время завода
    assert naive[0].utcoffset() == timedelta(hours=OFF)


def test_bucket_width_and_mode_choice():
    from trends import RAW_LIMIT, bucket_width_sec, use_raw
    assert bucket_width_sec(3600, 1000) == 4          # округление вверх: корзин не больше заказанных
    assert bucket_width_sec(10, 1000) == 1            # не мельче секунды
    assert bucket_width_sec(86400 * 93, 1000) == 8036
    assert use_raw(RAW_LIMIT) and not use_raw(RAW_LIMIT + 1)


def test_step_by_deadband():
    from trends import is_step
    assert is_step(1.0) and is_step(0.01)
    assert not is_step(None) and not is_step(0) and not is_step(-1)


def test_stats_helpers():
    from trends import stats_from_buckets, stats_from_raw
    assert stats_from_raw([]) == {"min": None, "avg": None, "max": None, "count": 0}
    assert stats_from_raw([1.0, 2.0, 6.0]) == {"min": 1.0, "avg": 3.0, "max": 6.0, "count": 3}
    # корзины: (min, avg, max, n); среднее взвешено по числу точек
    s = stats_from_buckets([(1.0, 2.0, 3.0, 1), (0.0, 5.0, 9.0, 3)])
    assert s == {"min": 0.0, "avg": pytest.approx(4.25), "max": 9.0, "count": 4}


def test_export_file_name():
    from trends import series_file_name
    d1 = datetime(2026, 10, 1, tzinfo=TZ)
    assert series_file_name("AE_WBS_2", d1, datetime(2026, 10, 7, tzinfo=TZ)) == "AE_WBS_2_2026-10-01_2026-10-06.xlsx"
    assert series_file_name("AE_WBS_2", d1, datetime(2026, 10, 2, tzinfo=TZ)) == "AE_WBS_2_2026-10-01.xlsx"
    assert series_file_name("AE/WBS:2", d1, datetime(2026, 10, 2, tzinfo=TZ)) == "AE_WBS_2_2026-10-01.xlsx"
    part = series_file_name("A", datetime(2026, 10, 8, 10, 5, tzinfo=TZ), datetime(2026, 10, 8, 10, 20, tzinfo=TZ))
    assert part == "A_2026-10-08_10-05_10-20.xlsx"


# ---------------------------------------------------------------- база
async def test_raw_when_few_points_and_stats(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc, lo=207, hi=253)
    await _put(pool, plc, t, [(T0 + timedelta(seconds=i), 230.0 + i) for i in range(5)])
    r = await _get(anon, t, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    assert r.status_code == 200, r.text
    s = r.json()["series"][0]
    assert s["mode"] == "raw" and s["step"] is False and s["gap_sec"] == 10
    assert [p[1] for p in s["points"]] == [230.0, 231.0, 232.0, 233.0, 234.0]
    assert s["points"][0][0] == int(T0.timestamp() * 1000)
    assert s["stats"] == {"min": 230.0, "avg": 232.0, "max": 234.0, "count": 5}
    assert (s["limit_low"], s["limit_high"]) == (207, 253) and s["prior"] is None
    assert r.json()["offset_hours"] == OFF


async def test_agg_when_many_points_min_avg_max_and_empty_buckets(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc)
    # 2500 точек за 2500 секунд: больше RAW_LIMIT, значит корзины; затем пропуск 5000 с и ещё одна точка
    rows = [(T0 + timedelta(seconds=i), float(i % 10)) for i in range(2500)]
    rows.append((T0 + timedelta(seconds=7500), 100.0))
    await _put(pool, plc, t, rows)
    f, to = T0, T0 + timedelta(seconds=8000)
    r = await _get(anon, t, f, to, buckets=100)
    assert r.status_code == 200, r.text
    j = r.json()
    s = j["series"][0]
    assert s["mode"] == "agg" and j["bucket_sec"] == 80
    pts = s["points"]
    assert len(pts) < 101 and all(len(p) == 5 for p in pts)
    first = pts[0]            # корзина [0, 80): точки 0..79, значения i % 10
    assert first[0] == int(T0.timestamp() * 1000) and first[1] == 0.0 and first[3] == 9.0 and first[4] == 80
    assert first[2] == pytest.approx(4.5)
    assert pts[-1][1] == pts[-1][3] == 100.0 and pts[-1][4] == 1
    starts = [p[0] for p in pts]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert max(starts) < int(to.timestamp() * 1000)
    assert sum(p[4] for p in pts) == 2501
    assert s["stats"]["count"] == 2501 and s["stats"]["max"] == 100.0 and s["stats"]["min"] == 0.0
    assert s["stats"]["avg"] == pytest.approx((sum(i % 10 for i in range(2500)) + 100) / 2501)


async def test_point_on_bucket_boundary_goes_to_next_bucket(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc)
    rows = [(T0 + timedelta(seconds=i * 0.01), 1.0) for i in range(2100)]    # 2100 точек за 21 с -> корзины
    rows.append((T0 + timedelta(seconds=40), 5.0))
    await _put(pool, plc, t, rows)
    r = await _get(anon, t, T0, T0 + timedelta(seconds=60), buckets=60)
    s = r.json()["series"][0]
    assert r.json()["bucket_sec"] == 1 and s["mode"] == "agg"
    by = {p[0]: p for p in s["points"]}
    base = int(T0.timestamp() * 1000)
    assert by[base + 40000][1] == 5.0 and by[base + 40000][4] == 1
    assert base + 39000 not in by            # пустая корзина не возвращается


async def test_step_series_has_prior_value(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc, deadband=1.0)
    await _put(pool, plc, t, [(T0 - timedelta(hours=3), 229.0), (T0 + timedelta(minutes=5), 231.0)])
    r = await _get(anon, t, T0, T0 + timedelta(hours=1))
    s = r.json()["series"][0]
    assert s["step"] is True and s["mode"] == "raw"
    assert s["prior"] == [int((T0 - timedelta(hours=3)).timestamp() * 1000), 229.0]
    assert [p[1] for p in s["points"]] == [231.0]


async def test_series_keep_requested_order_and_missing_tag(anon, pool):
    plc = await _plc(pool)
    a, b = await _tag(pool, plc, "A"), await _tag(pool, plc, "B")
    r = await _get(anon, f"{b},{a}", T0, T0 + timedelta(hours=1))
    assert [s["name"] for s in r.json()["series"]] == ["B", "A"]
    r = await _get(anon, f"{a},999999", T0, T0 + timedelta(hours=1))
    assert r.status_code == 422


async def test_validation_422(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc)
    f = T0
    assert (await _get(anon, t, f, f)).status_code == 422
    assert (await _get(anon, t, f, f + timedelta(days=94))).status_code == 422
    assert (await _get(anon, t, f, f + timedelta(days=93))).status_code == 200
    assert (await _get(anon, ",".join(str(i) for i in range(1, 10)), f, f + timedelta(hours=1))).status_code == 422
    assert (await _get(anon, "abc", f, f + timedelta(hours=1))).status_code == 422
    assert (await _get(anon, t, f, f + timedelta(hours=1), buckets=10)).status_code == 422
    assert (await _get(anon, t, f, f + timedelta(hours=1), buckets=5000)).status_code == 422
    assert (await anon.get(f"/api/trends/series?from=xx&to=yy&tags={t}")).status_code == 422
    assert (await anon.get(f"/api/trends/series?tags={t}")).status_code == 422


async def test_vars_list_open_for_everyone(anon, users, pool):
    p2, p1 = await _plc(pool, "P2", 2), await _plc(pool, "P1", 1)
    await _tag(pool, p2, "B")
    a = await _tag(pool, p1, "A", deadband=1.0, lo=1, hi=2)
    await _tag(pool, p1, "dig", ttype="DIGITAL")
    await _tag(pool, p1, "noarch", archived=False)
    for c in (anon, users["t_viewer"], users["t_admin"]):
        r = await c.get("/api/trends/vars")
        assert r.status_code == 200
        plcs = r.json()["plcs"]
        assert [p["name"] for p in plcs] == ["P1", "P2"]
        assert [t["name"] for t in plcs[0]["tags"]] == ["A"]
        t = plcs[0]["tags"][0]
        assert t["tag_id"] == a and t["deadband"] == 1.0 and t["limit_low"] == 1 and t["limit_high"] == 2
    assert (await anon.get(f"/api/trends/series?from=2026-10-08T10:00:00%2B05:00&to=2026-10-08T11:00:00%2B05:00&tags={a}")).status_code == 200


async def test_telemetry_index_exists(pool):
    rows = await pool.fetch("SELECT indexdef FROM pg_indexes WHERE tablename = 'telemetry'")
    assert any(re.search(r"\(tag_id, ts( DESC)?\)", r["indexdef"]) for r in rows)


async def test_ensure_schema_idempotent(pool):
    from trends import ensure_trends_schema
    await ensure_trends_schema(pool)
    await ensure_trends_schema(pool)


# ---------------------------------------------------------------- Excel
async def _xlsx(c, tags, f, t, plc=None):
    q = f"/api/trends/series.xlsx?from={iso(f).replace('+', '%2B')}&to={iso(t).replace('+', '%2B')}&tags={tags}&src=trends"
    if plc:
        q += f"&plc={plc}"
    return await c.get(q)


NSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_book(data: bytes):
    """Книга как {лист: строки (список словарей колонка -> значение)}, порядок листов и скрытость."""
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(data))
    sst = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NSX + "si"):
            sst.append("".join(t.text or "" for t in si.iter(NSX + "t")))
    wbx = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = [(s.get("name"), s.get("state")) for s in wbx.iter(NSX + "sheet")]
    out = {}
    for i, (name, _) in enumerate(sheets, start=1):
        rows = []
        for row in ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml")).iter(NSX + "row"):
            cells = {}
            for c in row.findall(NSX + "c"):
                v = c.find(NSX + "v")
                if v is None:
                    continue
                cells[re.sub(r"\d", "", c.get("r"))] = sst[int(v.text)] if c.get("t") == "s" else v.text
            rows.append(cells)
        out[name] = rows
    return z, sheets, out


async def test_excel_file_sheet_chart_and_names(anon, pool):
    from urllib.parse import unquote
    plc = await _plc(pool, "AE_WBS_2")
    a = await _tag(pool, plc, "Voltage L1-N")
    b = await _tag(pool, plc, "Voltage L2-N", deadband=1.0)
    await _put(pool, plc, a, [(T0 + timedelta(seconds=i), 230.0 + i % 3) for i in range(60)])
    await _put(pool, plc, b, [(T0 + timedelta(seconds=10), 231.0), (T0 + timedelta(seconds=30), 232.0)])
    f, t = T0, T0 + timedelta(minutes=2)
    r = await _xlsx(anon, f"{a},{b}", f, t, plc="AE_WBS_2")
    assert r.status_code == 200, r.text
    assert "Тренды_AE_WBS_2_2026-10-08_10-00_10-02.xlsx" in unquote(r.headers["content-disposition"])
    z, sheets, book = read_book(r.content)
    charts = [n for n in z.namelist() if n.startswith("xl/charts/chart")]
    assert len(charts) == 1
    xml = z.read(charts[0]).decode("utf-8")
    assert "Voltage L1-N" in xml and "Voltage L2-N" in xml and "Графики переменных" in xml
    assert sheets[0] == ("Графики переменных", None) and ("График", "hidden") in sheets
    rows = book["Графики переменных"]
    texts = {v for row in rows for v in row.values()}
    for text in ("Переменная", "Минимум", "Среднее", "Максимум", "Точек", "Статистика за видимый период"):
        assert text in texts, text
    row_a = next(x for x in rows if x.get("A") == "Voltage L1-N")
    assert float(row_a["B"]) == 230.0 and float(row_a["C"]) == pytest.approx(231.0) and float(row_a["D"]) == 232.0 and int(row_a["E"]) == 60
    hid = book["График"]
    assert [hid[0][k] for k in "ABC"] == ["Время", "Voltage L1-N", "Voltage L2-N"]


async def test_excel_validation_and_access(anon, pool):
    plc = await _plc(pool)
    t = await _tag(pool, plc)
    assert (await _xlsx(anon, t, T0, T0)).status_code == 422
    assert (await _xlsx(anon, "x", T0, T0 + timedelta(hours=1))).status_code == 422
    assert (await _xlsx(anon, t, T0, T0 + timedelta(hours=1))).status_code == 200
