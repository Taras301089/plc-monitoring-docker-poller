"""Итоги отставания по отделам (T-17): ПР / ОТО / ИТО / ДР, «не описано», «ждёт подтверждения», «спорно».

Одна серверная функция (responsibility.py) считает числа поверх общего расчёта отставания (summarize / dayflags); Andon, «Простои», «Отчёты»
и Excel берут их только оттуда. Сумма ПР + ОТО + ИТО + ДР + не описано всегда равна всему отставанию."""
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

import dayflags
import downtime
from test_autosave_single_sheet import _cells, _find_row
from test_no_output_day import OFF, TODAY, WORK, _hourly
from test_no_output_day import _seed as _seed_off
from test_responsibility import DAY, _item, _put, _seed

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = STATIC / "responsibility.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")
OWNERS = ("production", "oto", "ito", "other")
HEADS = ["", "Всего", "ПР", "ОТО", "ИТО", "ДР", "Не описано", "Ждёт подтверждения", "Спорно"]


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dayflags, "plant_today", lambda: TODAY)


def _src(name):
    return (STATIC / name).read_text(encoding="utf-8")


async def _resp(anon, scr, d1=DAY, d2=None):
    r = await anon.get(f"/api/kpi/screens/{scr}/responsibility", params={"from": d1.isoformat(), "to": (d2 or d1).isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def _identity(r):
    """Сумма отделов и «не описано» равна всему отставанию."""
    assert round(sum(r["owners"].values()) + r["undescribed"] - r["total"], 1) == 0


# ---------- функция расчёта (через API) ----------

async def test_totals_by_owner_and_undescribed(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="production"), _item(r_def, 5, owner="oto"), _item(r_def, 5, owner="ito")])
    r = await _resp(anon, scr)
    assert r["total"] == 30 and r["owners"] == {"production": 10, "oto": 5, "ito": 5, "other": 0}
    assert r["undescribed"] == 10
    assert r["pending"] == 10 and r["disputed"] == 0                 # ОТО и ИТО ждут подтверждения; «спорно» пока всегда 0
    assert r["pending_by"]["oto"] == 5 and r["pending_by"]["ito"] == 5 and r["pending_by"]["production"] == 0
    _identity(r)


async def test_pending_is_slice_not_addend(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 12, owner="oto"), _item(r_def, 8, owner="other", note="нет питания")])
    r = await _resp(anon, scr)
    assert r["owners"]["other"] == 8 and r["owners"]["oto"] == 12 and r["undescribed"] == 10
    assert r["pending"] == 12                                          # срез внутри отделов, слагаемым не является
    _identity(r)


async def test_shares_in_percent(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 15, owner="production"), _item(r_def, 15, owner="oto")])
    r = await _resp(anon, scr)
    assert r["share"]["production"] == 50.0 and r["share"]["oto"] == 50.0 and r["share"]["ito"] == 0.0


async def test_described_over_total_is_capped(app, users, anon, pool):
    """Строки описания могут превысить простой на минуту (разрешено при сохранении): сумма всё равно равна отставанию."""
    scr, dt, r_def, _ = await _seed(pool)
    assert (await _put(users["t_master"], dt, [_item(r_def, 15, owner="production"), _item(r_def, 16, owner="oto")])).status_code == 200
    r = await _resp(anon, scr)
    assert r["total"] == 30 and r["undescribed"] == 0 and sum(r["owners"].values()) == 30
    _identity(r)


async def test_period_boundaries(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 30, owner="production")])
    nxt = DAY + timedelta(days=1)
    await pool.execute("INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, 1, 480, 540, 10, 5)", scr, nxt)
    dt2 = await pool.fetchval(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 10, 5, 60, 20) RETURNING id", scr, nxt)
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, owner) VALUES ($1, $2, 20, 'ito')", dt2, r_def)
    assert (await _resp(anon, scr, DAY, DAY))["total"] == 30                        # граница: следующий день не входит
    r_next = await _resp(anon, scr, nxt, nxt)
    assert r_next["total"] == 20 and r_next["owners"]["ito"] == 20 and r_next["owners"]["production"] == 0
    both = await _resp(anon, scr, DAY, nxt)
    assert both["total"] == 50 and both["owners"]["production"] == 30 and both["owners"]["ito"] == 20
    _identity(both)


async def test_no_data_gives_zeros(app, anon, pool):
    scr, *_ = await _seed(pool)
    r = await _resp(anon, scr, date(2026, 9, 1))
    assert r["total"] == 0 and r["undescribed"] == 0 and r["owners"] == {k: 0 for k in OWNERS}
    assert r["share"]["production"] == 0


async def test_no_output_days_do_not_enter_totals(app, anon, pool):
    """День без выпуска: автоматический простой и «не описано» не входят, описанные людьми минуты остаются (правило dayflags)."""
    scr = await _seed_off(pool)
    r = await _resp(anon, scr, WORK, OFF)
    assert r["total"] == 15 and r["undescribed"] == 15                              # воскресенье (180 мин) не считается
    reason = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_r_off', '', 9998) RETURNING id")
    dt = await pool.fetchval("SELECT id FROM kpi_downtimes WHERE screen_id = $1 AND prod_date = $2 AND idx = 1", scr, OFF)
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, note, owner) VALUES ($1, $2, 7, 'описал человек', 'oto')", dt, reason)
    r = await _resp(anon, scr, WORK, OFF)
    assert r["total"] == 22 and r["owners"]["oto"] == 7 and r["undescribed"] == 15 and r["pending"] == 7
    _identity(r)


async def test_total_equals_tile_everywhere(app, users, anon, pool):
    """«Простой, мин» на «Графиках»/«Отчётах», плитка дня и Andon берут то же число, что итог по отделам."""
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="production"), _item(r_def, 5, owner="oto")])
    r = await _resp(anon, scr)
    ch = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": DAY.isoformat(), "to": DAY.isoformat()})).json()
    assert ch["totals"]["downtime"] == r["total"]
    assert round(ch["totals"]["downtime"] - ch["totals"]["described"], 1) == r["undescribed"]
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    down_col = pv["headers"].index("Отставание мин")
    tile = sum(float(row[down_col]) for row in pv["rows"] if isinstance(row[down_col], (int, float)) and row[2] == 1 and row[down_col] != "")
    assert tile == r["total"]
    assert pv["responsibility"] == r
    dl = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": DAY.isoformat()})).json()
    assert dl["responsibility"] == r                                                  # вместе со списком простоев дня (Andon без лишнего запроса)
    assert sum(d["minutes"] for d in dl["downtimes"]) == r["total"]


async def test_manual_screen_counts_its_own_downtimes(app, users, anon, pool):
    """У экрана подсборки своей почасовой истории нет, но его простои считаются."""
    main, *_ = await _seed(pool)
    plc = await pool.fetchval("SELECT plc_id FROM kpi_screens WHERE id = $1", main)
    man = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('GWM Floor', $2, 'manual_counter', $1::jsonb) RETURNING id",
        json.dumps({"brand": "GWM", "area": "Floor subassembly", "main": main}), plc)
    dt = await pool.fetchval(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 10, 4, 60, 24) RETURNING id", man, DAY)
    reason = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_r_man', '', 9997) RETURNING id")
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, owner) VALUES ($1, $2, 9, 'production')", dt, reason)
    r = await _resp(anon, man)
    assert r["total"] == 24 and r["owners"]["production"] == 9 and r["undescribed"] == 15


def test_disputed_is_a_separate_slice(monkeypatch):
    """«Спорно» берётся из статуса строки (шаг 2 задачи T-15 даст его реально): отдел остаётся в сумме, срез отдельно."""
    import responsibility
    from datetime import date as d
    data = {"days": [{"prod_date": d(2026, 10, 5), "plan": 10, "fact": 5}], "dt_minutes": {d(2026, 10, 5): 30.0}, "no_output": set(),
            "items": [{"prod_date": d(2026, 10, 5), "idx": 1, "area_id": None, "station_id": None, "reason_id": 1, "minutes": 10, "owner": "oto"},
                      {"prod_date": d(2026, 10, 5), "idx": 1, "area_id": None, "station_id": None, "reason_id": 1, "minutes": 6, "owner": "production"}],
            "areas": {}, "stations": {}, "reasons": {1: "r"}}
    monkeypatch.setattr(downtime, "owner_status", lambda o: "disputed" if o == "oto" else "confirmed")
    r = responsibility.owner_split(data)
    assert r["total"] == 30 and r["owners"]["oto"] == 10 and r["owners"]["production"] == 6 and r["undescribed"] == 14
    assert r["disputed"] == 10 and r["pending"] == 0 and r["disputed_by"]["oto"] == 10
    _identity(r)


# ---------- «Простои»: все экраны за период ----------

async def test_downtimes_list_has_responsibility_equal_to_rows(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="production"), _item(r_def, 5, owner="ito")])
    r = (await anon.get("/api/kpi/downtimes-list", params={"from": DAY.isoformat(), "to": DAY.isoformat()})).json()
    resp = r["responsibility"]
    assert resp == await _resp(anon, scr)                                              # одна функция: те же числа, что у экрана линии
    rows = r["rows"]
    assert resp["total"] == sum(x["item_minutes"] for x in rows)                       # плитка «всего минут» = итог блока
    assert resp["undescribed"] == sum(x["item_minutes"] for x in rows if x["kind"] == "rest")
    for k in OWNERS:
        assert resp["owners"][k] == sum(x["item_minutes"] for x in rows if x["kind"] == "item" and x["owner"] == k)


# ---------- Excel = экран ----------

def _block(content):
    cells = _cells(content)
    r0 = _find_row(cells, "По ответственным", "A", startswith=False)
    heads = [cells.get((r0 + 1, c)) or "" for c in "ABCDEFGHI"]
    mins = [cells.get((r0 + 2, c)) for c in "ABCDEFGHI"]
    share = [cells.get((r0 + 3, c)) for c in "ABCDEFGHI"]
    return heads, mins, share


async def _seeded(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="production"), _item(r_def, 5, owner="oto")])
    return scr


@pytest.mark.parametrize("url,params", [
    ("day.xlsx", {"date": DAY.isoformat(), "src": "reports"}),
    ("day.xlsx", {"date": DAY.isoformat(), "src": "andon"}),
    ("period.xlsx", {"from": DAY.isoformat(), "to": DAY.isoformat(), "src": "reports"}),
])
async def test_excel_block_equals_screen(app, users, anon, pool, url, params):
    scr = await _seeded(app, users, pool)
    r = await _resp(anon, scr)
    xl = await anon.get(f"/api/kpi/screens/{scr}/{url}", params=params)
    assert xl.status_code == 200
    heads, mins, share = _block(xl.content)
    assert heads == HEADS                                                              # заголовки те же, что в responsibility.js
    assert mins[0] == "Минут"
    assert mins[1:] == [r["total"], r["owners"]["production"], r["owners"]["oto"], r["owners"]["ito"], r["owners"]["other"],
                        r["undescribed"], r["pending"], r["disputed"]]
    assert share[0] == "Доля, %"
    assert round(share[2] * 100, 1) == r["share"]["production"] and round(share[3] * 100, 1) == r["share"]["oto"]


async def test_excel_total_is_formula_of_owners(app, users, anon, pool):
    scr = await _seeded(app, users, pool)
    xl = await anon.get(f"/api/kpi/screens/{scr}/period.xlsx", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    import io
    import zipfile
    sheet = zipfile.ZipFile(io.BytesIO(xl.content)).read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells = _cells(xl.content)
    row = _find_row(cells, "По ответственным", "A", startswith=False) + 2
    assert re.search(rf'<c r="B{row}"[^>]*><f>C{row}\+D{row}\+E{row}\+F{row}\+G{row}</f>', sheet)


async def test_excel_block_empty_day_has_no_error(app, anon, pool):
    scr, *_ = await _seed(pool)
    xl = await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})
    assert xl.status_code == 200
    heads, mins, _ = _block(xl.content)
    assert mins[1] == 30 and mins[6] == 30                                             # всё «не описано»


# ---------- экран: исходники и чистые функции в node ----------

def test_screen_sources_use_responsibility():
    assert 'src="/static/responsibility.js"' in _src("index.html")
    assert _src("index.html").index("responsibility.js") < _src("index.html").index("/static/downtime.js")
    assert _src("index.html").count('data-k="resp"') == 2 and _src("manual.js").count('data-k="resp"') == 1   # ИТОГО: Main Line, FL, подсборки
    assert "respKit?.fill(" in _src("downtime.js") and "responsibility" in _src("downtime.js")
    assert "tfoot [data-k=\"resp\"]" in _src("responsibility.js")
    assert "responsibility" in _src("downtimes.js") and "Минуты по ответственным" in _src("downtimes.js")
    assert "По ответственным" in _src("reports.js") and "responsibility" in _src("reports.js")
    # тайм-аут запроса простоев Andon не больше 6 секунд
    body = _src("downtime.js")[_src("downtime.js").index("async function load("):]
    assert "AbortSignal.timeout(6000)" in body[:700]
    assert ".resp-sum" in _src("downtime.css")


@node
def test_js_heads_equal_excel_heads():
    out = subprocess.run(["node", "-e", f"console.log(JSON.stringify(require({json.dumps(str(JS))}).HEADS))"],
                         capture_output=True, text=True, timeout=30, check=True, encoding="utf-8").stdout
    assert json.loads(out) == HEADS
    import responsibility
    assert list(responsibility.HEADS) == HEADS


def run(expr):
    code = f"const r = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True, encoding="utf-8").stdout)


R = {"total": 20, "owners": {"production": 15, "oto": 5, "ito": 0, "other": 0}, "undescribed": 0, "pending": 5, "disputed": 0,
     "pending_by": {"production": 0, "oto": 5, "ito": 0, "other": 0}, "disputed_by": {"production": 0, "oto": 0, "ito": 0, "other": 0},
     "share": {"production": 75.0, "oto": 25.0, "ito": 0.0, "other": 0.0, "undescribed": 0.0, "pending": 25.0, "disputed": 0.0}}


@node
def test_andon_line_text_and_title():
    got = run(f"r.andonLine({json.dumps(R)})")
    assert got["text"] == "Отставание за день 20 мин: ПР 15 · ОТО 5 …"                 # только отделы с минутами; «…» у отдела с ожиданием
    assert "ИТО" not in got["text"] and "не описано" not in got["text"]
    for part in ("Производство", "ОТО", "ждёт подтверждения", "Не описано"):
        assert part in got["title"], part
    assert "resp-disp" not in got["html"]


@node
def test_andon_line_undescribed_and_disputed():
    r = {**R, "total": 30, "undescribed": 10, "disputed": 5, "pending": 0,
         "pending_by": {**R["pending_by"], "oto": 0}, "disputed_by": {**R["disputed_by"], "oto": 5}}
    got = run(f"r.andonLine({json.dumps(r)})")
    assert got["text"] == "Отставание за день 30 мин: ПР 15 · ОТО 5 ! · не описано 10"
    assert 'class="resp-disp"' in got["html"]                                          # красным только «спорно»


@node
def test_andon_line_empty_when_no_lag_or_no_data():
    assert run(f"r.andonLine({json.dumps({**R, 'total': 0, 'owners': {k: 0 for k in OWNERS}, 'pending': 0})})")["text"] == ""
    assert run("r.andonLine(null)")["text"] == ""


@node
def test_table_rows_like_server():
    rows = run(f"r.tableRows({json.dumps(R)})")
    assert rows[0] == ["Минут", "20", "15", "5", "0", "0", "0", "5", "0"]
    assert rows[1] == ["Доля, %", "100%", "75%", "25%", "0%", "0%", "0%", "25%", "0%"]
