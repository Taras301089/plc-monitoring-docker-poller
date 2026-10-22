"""Галочки интервалов у Finish Line (fl_counter): окно «Время интервалов», сохранение kpi_schedule.active,
скрытие пустых интервалов на экране, в архиве, в Excel и в предпросмотре (так же, как у body_counter)."""
import io
import json
import re
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
ARCHIVE = (ROOT / "fastapi-api" / "static" / "archive.js").read_text(encoding="utf-8")
DAY = date(2026, 10, 5)
TIP = "Показывать интервал на экране; интервалы без галочки скрыты (если в них нет кузовов)"


def _between(text, start, end):
    i = text.index(start)
    return text[i: text.index(end, i)]


# ---------- окно «Время интервалов» ----------

def test_fl_dialog_has_checkbox_column_and_hint():
    body = _between(HTML, "const isFl = screen.template === 'fl_counter';", "document.body.appendChild(modal);")
    assert 'data-on="${i}"' in body                                   # у каждого интервала своя галочка
    assert TIP in body                                                # подсказка колонки и чекбокса
    assert "isFl ? `<th" in body or "isFl ? '<th" in body             # колонка есть только у ФЛ
    assert "active: isFl ? modal.querySelector(`[data-on=" in body           # галочка уходит на сервер тем же полем active
    assert "не показываются на экране" in body                        # подсказка в окне про галочки


def test_fl_dialog_does_not_send_active_for_other_templates():
    body = _between(HTML, "const isFl = screen.template === 'fl_counter';", "document.body.appendChild(modal);")
    assert re.search(r"active: isFl \? .*? : undefined", body)       # у остальных линий поле не передаётся: остаётся как было


# ---------- экран и архив ----------

def test_fl_screen_hides_unchecked_empty_interval():
    paint = _between(HTML, "function paintFl(screen, v) {", "function setAndonBadge")
    # интервал без галочки скрыт, но виден, если в нём есть факт или план из ПЛК
    assert re.search(r"tr\.hidden = !cfg\.active && fact === 0 && plan === 0", paint)


def test_fl_screen_sums_skip_hidden_rows():
    paint = _between(HTML, "function paintFl(screen, v) {", "function setAndonBadge")
    # итоги простоев и ремонта считаются по показанным строкам, как таблица Excel
    assert "tr.hidden" in paint[paint.index("starveVals.push") - 600: paint.index("starveVals.push")]


def test_archive_shows_only_intervals_with_plan_or_fact_for_every_template():
    assert "data.intervals.filter(x => x.plan > 0 || x.fact > 0)" in ARCHIVE


# ---------- сервер ----------

async def _fl(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_fl_act', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    return await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('FL RB262', $1, 'fl_counter', '{}'::jsonb) RETURNING id", plc)


def _items(active):
    return [{"idx": i + 1, "end_min": 540 + 60 * i, "plan": 0, **({} if a is None else {"active": a})} for i, a in enumerate(active)]


async def test_server_saves_and_returns_active_for_fl(app, pool, users):
    scr = await _fl(pool)
    r = await users["t_master"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([True, False, True])})
    assert r.status_code == 200
    got = (await users["t_viewer"].get(f"/api/kpi/screens/{scr}/schedule")).json()
    assert [x["active"] for x in got] == [True, False, True]
    # поле не передано: остаётся как было
    await users["t_master"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([None, None, None])})
    got = (await users["t_viewer"].get(f"/api/kpi/screens/{scr}/schedule")).json()
    assert [x["active"] for x in got] == [True, False, True]


async def test_active_rights_same_as_other_lines(app, pool, users, anon):
    scr = await _fl(pool)
    assert (await anon.put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([False])})).status_code == 401
    assert (await users["t_viewer"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([False])})).status_code == 403
    assert (await users["t_oto"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([False])})).status_code == 200


async def _hourly(pool, scr, rows):
    for i, (plan, fact) in enumerate(rows, start=1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            scr, DAY, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), plan, fact)


async def test_hidden_empty_interval_not_in_preview_but_interval_with_fact_or_plan_is(app, pool, users, anon):
    scr = await _fl(pool)
    # 1: план и факт, 2: пусто (скрыт галочкой), 3: только факт (скрыт галочкой), 4: только план (скрыт галочкой)
    await _hourly(pool, scr, [(5, 4), (0, 0), (0, 2), (3, 0)])
    await users["t_master"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([True, False, False, False])})
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    h = pv["headers"]
    assert [r[h.index("№")] for r in pv["rows"]] == [1, 3, 4]
    assert [(i["plan"], i["fact"]) for i in pv["intervals"]] == [(5, 4), (0, 2), (3, 0)]


async def test_excel_rows_equal_preview_rows_for_fl(app, pool, users, anon):
    scr = await _fl(pool)
    await _hourly(pool, scr, [(5, 4), (0, 0), (0, 2)])
    await users["t_master"].put(f"/api/kpi/screens/{scr}/schedule", json={"items": _items([True, False, False])})
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content
    z = zipfile.ZipFile(io.BytesIO(book))
    shared = [re.sub(r"<[^>]+>", "", t) for t in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    # номера интервалов и время в файле те же, что в предпросмотре: пустой интервал 2 (09:00) в файл не попал
    assert [r[pv["headers"].index("Начало")] for r in pv["rows"]] == ["08:00", "10:00"]
    assert "08:00-09:00" in shared and "10:00-11:00" in shared and "09:00-10:00" not in shared   # и в подписях графика
