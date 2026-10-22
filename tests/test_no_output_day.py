"""Дни без выпуска не считаются простоем; отметка «Простой весь день (поломка линии)».

Закрытый день, в котором на линии не произведено ни одного кузова, это выходной или нет плана: автоматический простой и «не описано»
в статистику не идут ни на одном экране (Andon, «Простои», «Графики», «Отчёты», Excel, автосохранение). Описанные людьми строки остаются."""
import io
import re
import zipfile
from datetime import date

import pytest

import dayflags

TODAY = date(2026, 10, 12)       # «сегодня» в тестах
OFF = date(2026, 10, 11)         # воскресенье: закрытый день без выпуска
WORK = date(2026, 10, 5)         # рабочий день с выпуском
PLAN = [4, 4, 5]


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dayflags, "plant_today", lambda: TODAY)


async def _hourly(pool, scr, day, facts, plans=PLAN, downtime=True):
    for i, (p, f) in enumerate(zip(plans, facts), start=1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), p, f)
        if downtime and f < p:
            await pool.execute(
                "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, $3, $4, $5, 60, $6)",
                scr, day, i, p, f, round((p - f) / p * 60, 1))


async def _seed(pool):
    """Линия GWM: воскресенье без выпуска (простой 60+60+60 = 180 мин), рабочий день (простой 15 + 0 + 0), сегодня без выпуска (180)."""
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    await _hourly(pool, scr, OFF, [0, 0, 0])
    await _hourly(pool, scr, WORK, [3, 4, 5])
    await _hourly(pool, scr, TODAY, [0, 0, 0])
    return scr


async def _describe(pool, scr, day, idx, minutes):
    reason = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_r' || $1::text, '', 9999) RETURNING id", str(idx))
    dt = await pool.fetchval("SELECT id FROM kpi_downtimes WHERE screen_id = $1 AND prod_date = $2 AND idx = $3", scr, day, idx)
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, note) VALUES ($1, $2, $3, 'описал человек')", dt, reason, minutes)


# ---------- чтение xlsx без openpyxl ----------

def _shared(content: bytes) -> list[str]:
    z = zipfile.ZipFile(io.BytesIO(content))
    xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    return ["".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)) for si in re.findall(r"<si>(.*?)</si>", xml, re.S)]


def _cells(content: bytes) -> dict[str, object]:
    z = zipfile.ZipFile(io.BytesIO(content))
    shared = _shared(content)
    out: dict[str, object] = {}
    for m in re.finditer(r'<c r="([A-Z]+\d+)"([^>]*?)(?:/>|>(.*?)</c>)', z.read("xl/worksheets/sheet1.xml").decode("utf-8"), re.S):
        v = re.search(r"<v>(.*?)</v>", m.group(3) or "", re.S)
        if v:
            out[m.group(1)] = shared[int(v.group(1))] if 't="s"' in m.group(2) else (v.group(1) if 't="str"' in m.group(2) else float(v.group(1)))
    return out


def _tile_value(cells: dict[str, object], label: str):
    ref = next(k for k, v in cells.items() if v == label and re.fullmatch(r"[A-Z]+3", k))
    return cells[ref[:-1] + "4"]


# ---------- единое правило ----------

def test_rule_is_closed_day_with_zero_output_and_no_flag():
    f = dayflags.is_no_output_day
    assert f(OFF, 0, False, TODAY) is True
    assert f(WORK, 12, False, TODAY) is False        # был выпуск
    assert f(TODAY, 0, False, TODAY) is False        # текущий день до закрытия правило не применяет
    assert f(date(2026, 10, 13), 0, False, TODAY) is False
    assert f(OFF, 0, True, TODAY) is False           # отмечен «простой весь день»
    assert f(OFF, None, False, TODAY) is False       # фактических данных нет


async def test_day_states_only_closed_zero_days(app, pool):
    scr = await _seed(pool)
    st = await dayflags.day_states(pool, WORK, TODAY, scr)
    assert set(st) == {(scr, OFF)} and st[(scr, OFF)]["state"] == dayflags.NO_OUTPUT
    assert await dayflags.no_output_days(pool, WORK, TODAY) == {(scr, OFF)}


# ---------- потребители: Andon и архив ----------

async def test_andon_downtimes_hourly_and_archive_data(app, anon, pool):
    scr = await _seed(pool)
    off = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": OFF.isoformat()})).json()
    assert off["downtimes"] == [] and off["no_output"] is True
    work = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": WORK.isoformat()})).json()
    assert [d["minutes"] for d in work["downtimes"]] == [15.0] and work["no_output"] is False
    # сегодняшний день правило не применяет
    today = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": TODAY.isoformat()})).json()
    assert sum(d["minutes"] for d in today["downtimes"]) == 180 and today["no_output"] is False
    # данные архива Andon: день помечен, цифры плана и факта не тронуты
    h = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": OFF.isoformat()})).json()
    assert h["no_output"] is True and h["note"] and [i["plan"] for i in h["intervals"]] == PLAN
    assert (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": WORK.isoformat()})).json()["no_output"] is False


async def test_described_downtime_stays_on_no_output_day(app, anon, pool):
    scr = await _seed(pool)
    await _describe(pool, scr, OFF, 1, 30)
    d = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": OFF.isoformat()})).json()["downtimes"]
    assert len(d) == 1 and d[0]["idx"] == 1 and d[0]["described"] == 30 and d[0]["minutes"] == 30   # «не описано» нет
    days = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": OFF.isoformat(), "to": OFF.isoformat()})).json()
    assert days["days"][0]["downtime"] == 30 and days["days"][0]["described"] == 30
    rows = (await anon.get("/api/kpi/downtimes-list", params={"from": OFF.isoformat(), "to": OFF.isoformat()})).json()["rows"]
    assert [(r["kind"], r["item_minutes"]) for r in rows] == [("item", 30.0)]


# ---------- «Простои» ----------

async def test_downtimes_tab_skips_no_output_days(app, anon, pool):
    await _seed(pool)
    rows = (await anon.get("/api/kpi/downtimes-list", params={"from": WORK.isoformat(), "to": TODAY.isoformat()})).json()["rows"]
    assert {r["date"] for r in rows} == {WORK.isoformat(), TODAY.isoformat()}
    assert sum(r["item_minutes"] for r in rows if r["date"] == WORK.isoformat()) == 15


# ---------- «Графики» ----------

async def test_charts_exclude_no_output_downtime_and_not_described(app, anon, pool):
    scr = await _seed(pool)
    r = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": WORK.isoformat(), "to": OFF.isoformat()})).json()
    by = {d["date"]: d for d in r["days"]}
    assert by[WORK.isoformat()]["downtime"] == 15 and by[OFF.isoformat()]["downtime"] == 0 and by[OFF.isoformat()]["no_output"] is True
    assert by[WORK.isoformat()]["no_output"] is False
    assert sum(x["min"] for x in r["reasons"]) == 15       # Парето: «не описан» только рабочего дня
    assert sum(x["min"] for x in r["stations"]) == 15
    # план и факт дня без выпуска остаются как есть
    assert (by[OFF.isoformat()]["plan"], by[OFF.isoformat()]["fact"]) == (13, 0)


# ---------- «Отчёты» и Excel ----------

async def test_reports_day_preview_and_excel_for_no_output_day(app, anon, pool):
    scr = await _seed(pool)
    p = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": OFF.isoformat()})).json()
    assert p["no_output"] is True and p["state"] == dayflags.NO_OUTPUT and p["note"].startswith(dayflags.NO_OUTPUT_LABEL)
    h = p["headers"]
    down, delta = h.index("Отставание мин"), h.index("±")
    assert all(not isinstance(r[down], (int, float)) or r[down] == 0 for r in p["rows"])
    assert all(r[delta] == "" for r in p["rows"])                      # «±» не показывается
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": OFF.isoformat()})).content
    cells = _cells(book)
    assert dayflags.NO_OUTPUT_LABEL in _shared(book)
    assert _tile_value(cells, "Отставание, мин") == dayflags.NO_OUTPUT_LABEL
    # состав плиток дня без выпуска тот же, что на экране «Отчёты» (подписи плиток не меняются)
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "reports.js").read_text(encoding="utf-8")
    day_src = js[js.index("async function drawDay"):js.index("async function drawPeriod")]
    screen_tiles = re.findall(r"tile\(L\('[^']+',\s*'([^']+)'", day_src)
    sheet_tiles = [v for k, v in cells.items() if re.fullmatch(r"[A-Z]+3", k) and isinstance(v, str)]
    assert sheet_tiles == screen_tiles
    # у рабочего дня подписи нет
    book2 = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": WORK.isoformat()})).content
    assert not any(dayflags.NO_OUTPUT_LABEL in s for s in _shared(book2))
    assert _tile_value(_cells(book2), "Отставание, мин") == 15.0


async def test_reports_period_excel_and_autosave_exclude_no_output(app, anon, pool):
    from combined import build_combined_report
    scr = await _seed(pool)
    book = (await anon.get(f"/api/kpi/screens/{scr}/period.xlsx", params={"from": WORK.isoformat(), "to": OFF.isoformat()})).content
    cells = _cells(book)
    assert _tile_value(cells, "Отставание, мин") == 15.0 and _tile_value(cells, "Не описано, мин") == 15.0
    assert dayflags.NO_OUTPUT_LABEL in _shared(book)
    # автосохранение: файл за воскресенье, плитка простоя дня без выпуска подписана, месяц считает без этих минут
    data, _, _, n = await build_combined_report(pool, scr, OFF)
    assert n == 3 and dayflags.NO_OUTPUT_LABEL in _shared(data)
    cells = _cells(data)
    tiles = []
    for ref, v in cells.items():
        if v == "Отставание, мин":                       # плитка дня и плитка «с начала месяца»
            col, row = re.match(r"([A-Z]+)(\d+)", ref).groups()
            tiles.append(cells[f"{col}{int(row) + 1}"])
    # плитка дня подписана «нет выпуска», месячная плитка и таблица «По дням» считают только рабочий день (15 мин)
    assert dayflags.NO_OUTPUT_LABEL in tiles and tiles.count(15.0) == 2 and 180.0 not in tiles


# ---------- подсборки (ручной ввод) ----------

async def test_manual_screen_uses_manual_facts(app, anon, pool):
    main = await _seed(pool)
    plc = await pool.fetchval("SELECT plc_id FROM kpi_screens WHERE id = $1", main)
    m = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('GWM Body side', $1, 'manual_counter', $2::jsonb) RETURNING id",
        plc, f'{{"brand": "GWM", "area": "Body side", "main": "{main}"}}')
    for i in (1, 2):
        await pool.execute("INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, $3, 5, 0, 60, 60)", m, OFF, i)
    # факт ещё не вводили: правило не применяется
    r = (await anon.get(f"/api/kpi/screens/{m}/downtimes", params={"date": OFF.isoformat()})).json()
    assert len(r["downtimes"]) == 2 and r["no_output"] is False
    # ввели нули по интервалам: выходной
    for i in (1, 2):
        await pool.execute("INSERT INTO kpi_manual_hourly (screen_id, prod_date, idx, plan, fact) VALUES ($1, $2, $3, 5, 0)", m, OFF, i)
    r = (await anon.get(f"/api/kpi/screens/{m}/downtimes", params={"date": OFF.isoformat()})).json()
    assert r["downtimes"] == [] and r["no_output"] is True


# ---------- отметка «Простой весь день (поломка линии)» ----------

def _url(scr, day):
    return f"/api/kpi/screens/{scr}/day-flag/{day.isoformat()}"


async def test_flag_permissions_and_conflicts(app, anon, users, pool):
    scr = await _seed(pool)
    assert (await anon.put(_url(scr, OFF))).status_code == 401
    assert (await anon.delete(_url(scr, OFF))).status_code == 401
    assert (await users["t_viewer"].put(_url(scr, OFF))).status_code == 403
    r = await users["t_master"].put(_url(scr, WORK))
    assert r.status_code == 409 and "выпуск" in r.json()["detail"]
    r = await users["t_master"].put(_url(scr, TODAY))
    assert r.status_code == 409 and "закрыт" in r.json()["detail"]
    assert (await users["t_master"].put(_url(0 + scr + 999, OFF))).status_code == 404
    assert (await users["t_master"].delete(_url(scr, OFF))).status_code == 404       # снимать нечего


async def test_flag_puts_downtime_into_statistics_everywhere(app, anon, users, pool):
    scr = await _seed(pool)
    assert (await users["t_master"].put(_url(scr, OFF))).status_code == 200
    assert (await users["t_master"].put(_url(scr, OFF))).json()["status"] == "unchanged"
    d = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": OFF.isoformat()})).json()
    assert d["no_output"] is False and sum(x["minutes"] for x in d["downtimes"]) == 180
    ch = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": OFF.isoformat(), "to": OFF.isoformat()})).json()
    assert ch["days"][0]["downtime"] == 180 and ch["days"][0]["no_output"] is False
    assert sum(x["min"] for x in ch["reasons"]) == 180
    lst = (await anon.get("/api/kpi/downtimes-list", params={"from": OFF.isoformat(), "to": OFF.isoformat()})).json()["rows"]
    assert sum(r["item_minutes"] for r in lst) == 180
    p = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": OFF.isoformat()})).json()
    assert p["no_output"] is False and p["state"] == dayflags.FULL_DOWNTIME and p["note"].startswith(dayflags.FULL_DOWNTIME_LABEL)
    assert "T_MASTER" in p["note"] and "Мастер" in p["note"]                       # «Фамилия, должность» после содержания
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": OFF.isoformat()})).content
    assert any(s.startswith(dayflags.FULL_DOWNTIME_LABEL) for s in _shared(book))
    assert _tile_value(_cells(book), "Отставание, мин") == 180.0
    fl = (await anon.get("/api/kpi/day-flags", params={"screen_id": scr, "from": WORK.isoformat(), "to": TODAY.isoformat()})).json()
    assert [(x["date"], x["state"]) for x in fl["days"]] == [(OFF.isoformat(), dayflags.FULL_DOWNTIME)]
    assert fl["days"][0]["flag"]["set_by_name"].startswith("T_MASTER, Мастер")


async def test_flag_removal_rights_and_journal(app, anon, users, pool):
    scr = await _seed(pool)
    assert (await users["t_master"].put(_url(scr, OFF))).status_code == 200
    assert (await users["t_area"].delete(_url(scr, OFF))).status_code == 403         # чужую отметку снимает только автор или админ
    assert (await users["t_master"].delete(_url(scr, OFF))).status_code == 200      # автор
    d = (await anon.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": OFF.isoformat()})).json()
    assert d["no_output"] is True and d["downtimes"] == []
    assert (await users["t_chief"].put(_url(scr, OFF))).status_code == 200
    assert (await users["t_admin"].delete(_url(scr, OFF))).status_code == 200        # администратор
    log = await pool.fetch("SELECT old_value, new_value, user_name FROM kpi_day_flags_log WHERE screen_id = $1 ORDER BY id", scr)
    assert [(r["old_value"], r["new_value"]) for r in log] == [(None, True), (True, None), (None, True), (True, None)]
    assert log[0]["user_name"].startswith("T_MASTER") and log[3]["user_name"].startswith("T_ADMIN")
    assert await pool.fetchval("SELECT count(*) FROM kpi_day_flags") == 0             # отметка снята, журнал остался


async def test_new_tables_created_idempotently(app, pool):
    await dayflags.ensure_dayflags_schema(pool)
    await dayflags.ensure_dayflags_schema(pool)
    cols = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'kpi_day_flags'")}
    assert {"screen_id", "prod_date", "full_downtime", "set_by", "set_at"} <= cols
    assert await pool.fetchval("SELECT to_regclass('kpi_day_flags_log')") is not None


# ---------- все места расчёта вызывают единую точку ----------

def test_every_consumer_goes_through_the_single_rule():
    from pathlib import Path
    api = Path(__file__).resolve().parent.parent / "fastapi-api"
    for name in ("downtime.py", "period.py", "history.py"):
        assert "dayflags" in (api / name).read_text(encoding="utf-8"), name
    assert "ensure_dayflags_schema" in (api / "main.py").read_text(encoding="utf-8")
    assert "COPY fastapi-api/*.py ./" in (api / "Dockerfile").read_text(encoding="utf-8")   # dayflags.py попадает в образ общим COPY
