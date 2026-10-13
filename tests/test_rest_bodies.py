"""Строка «Остаток кузовов на ФЛ» (экраны FL RB262 и FL RB294): привязки, чтение, экран Andon и Excel."""
import io
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
sys.path.insert(0, str(ROOT / "fastapi-api"))

import pytest  # noqa: E402

import rest_bodies as rb  # noqa: E402

TITLE = "Остаток кузовов на ФЛ"
HINT = "Текущее число кузовов на фуллайне (ФЛ) по моделям, значение читается из ПЛК"
DAY = date(2026, 10, 5)
PARENT = 'ns=3;s="DB_TotalBodiesList"."WBS2_Model_263_294"'


# ---------- узлы и значения ----------

def test_node_ids_and_model_order():
    assert list(rb.REST_FIELDS) == ["A01", "P01", "CS55", "A13T"]     # порядок столбцов моделей на экране
    assert rb.rest_node_id("A01") == PARENT + '."Model4"'
    assert rb.rest_node_id("P01") == PARENT + '."Model5"'
    assert rb.rest_node_id("CS55") == PARENT + '."Model7"'
    assert rb.rest_node_id("A13T") == PARENT + '."Model1"'
    assert rb.REST_TITLE == TITLE and rb.REST_HINT == HINT


def _bind():
    return {f"rest_{m}": rb.rest_node_id(m) for m in rb.REST_FIELDS}


def test_values_by_model_and_no_value_is_none():
    b = _bind()
    v = {b["rest_A01"]: {"ok": True, "v": 3}, b["rest_P01"]: {"ok": True, "v": 0},
         b["rest_CS55"]: {"ok": False, "v": None}, b["rest_A13T"]: {"ok": True, "v": True}}
    assert rb.rest_from_values(b, v) == {"A01": 3, "P01": 0, "CS55": None, "A13T": None}
    assert rb.rest_from_values(b, {}) == {"A01": None, "P01": None, "CS55": None, "A13T": None}


def test_has_rest_requires_all_four():
    assert rb.has_rest(_bind())
    assert not rb.has_rest({"prod": "x"})
    assert not rb.has_rest({k: v for k, v in _bind().items() if k != "rest_CS55"})


# ---------- привязки ----------

async def _screen(pool, name="FL RB262", bound=False, plc=None):
    if plc is None:
        plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://127.0.0.1:4840') RETURNING id", f"r_{name}")
    b = {"prod": 'ns=3;s="P"', "starts": 'ns=3;s="S"'}
    if bound:
        b.update(_bind())
    scr = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ($1, $2, 'fl_counter', $3::jsonb) RETURNING id",
        name, plc, json.dumps(b))
    return plc, scr


async def _bindings(pool, scr):
    raw = await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr)
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def test_migration_only_for_two_screens_when_plc_knows_the_structure(app, pool):
    plc, s262 = await _screen(pool, "FL RB262")
    _, s294 = await _screen(pool, "FL RB294", plc=plc)
    _, other = await _screen(pool, "FL OTHER", plc=plc)
    assert await rb.ensure_rest_bindings(pool) == 0                   # ПЛК ещё не знает структуру
    await pool.execute(
        "INSERT INTO plc_discovered_nodes (plc_id, db_name, variable_name, node_id, browse_path) VALUES ($1, 'DB_TotalBodiesList', 'Model4', $2, 'x')",
        plc, PARENT + '."Model4"')
    assert await rb.ensure_rest_bindings(pool) == 2
    for scr in (s262, s294):
        b = await _bindings(pool, scr)
        assert {k: b[k] for k in _bind()} == _bind() and b["prod"]    # прежние привязки остались
    assert "rest_A01" not in await _bindings(pool, other)             # чужой экран не тронут
    assert await rb.ensure_rest_bindings(pool) == 0                   # повтор ничего не меняет


# ---------- чтение для Excel ----------

async def test_fetch_rest_reads_through_poller_only_for_today(monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)
    calls = []

    async def fake(plc_id, node_ids):
        calls.append((plc_id, node_ids))
        return {n: {"ok": True, "v": i + 1} for i, n in enumerate(node_ids)}

    monkeypatch.setattr(rb, "read_poller_values", fake)
    assert await rb.fetch_rest(8, _bind(), DAY) == {"A01": 1, "P01": 2, "CS55": 3, "A13T": 4}
    assert calls == [(8, [rb.rest_node_id(m) for m in rb.REST_FIELDS])]
    assert await rb.fetch_rest(8, _bind(), date(2026, 10, 4)) is None   # прошлый день: строки нет
    assert await rb.fetch_rest(8, {"prod": "x"}, DAY) is None             # нет привязки: строки нет
    assert len(calls) == 1


async def test_fetch_rest_without_link_gives_dashes(monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)

    async def boom(plc_id, node_ids):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(rb, "read_poller_values", boom)
    assert await rb.fetch_rest(8, _bind(), DAY) == {"A01": None, "P01": None, "CS55": None, "A13T": None}


# ---------- Excel ----------

async def _hourly(pool, scr, day=DAY):
    for i in (1, 2, 3):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1,$2,$3,$4,$5,4,3)",
            scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1))


def _sheet(book):
    z = zipfile.ZipFile(io.BytesIO(book))
    shared = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells = {}
    for ref, attrs, body in re.findall(r'<c r="([A-Z]+\d+)"([^>/]*)>(.*?)</c>', xml, re.S):
        v = re.search(r"<v>(.*?)</v>", body)
        if v:
            cells[ref] = shared[int(v.group(1))] if 't="s"' in attrs else float(v.group(1))
    return cells, xml


def _find(cells, text):
    ref = next(r for r, v in cells.items() if v == text)
    col, row = re.match(r"([A-Z]+)(\d+)", ref).groups()
    return col, int(row)


def _row_values(cells, row):
    return [v for r, v in sorted(((r, v) for r, v in cells.items() if re.fullmatch(r"[A-Z]+" + str(row), r)),
                                 key=lambda x: (len(re.match(r"[A-Z]+", x[0]).group(0)), x[0]))]


@pytest.fixture
def live_today(monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)

    async def fake(plc_id, node_ids):
        return {n: {"ok": True, "v": i + 5} for i, n in enumerate(node_ids)}

    monkeypatch.setattr(rb, "read_poller_values", fake)


async def test_excel_row_under_total_not_in_sum(app, anon, pool, live_today):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content
    cells, xml = _sheet(book)
    _, total_row = _find(cells, "Итого")
    _, rest_row = _find(cells, TITLE)
    assert rest_row == total_row + 1                                 # сразу под строкой «Итого»
    vals = _row_values(cells, rest_row)
    assert vals == [TITLE, 5.0, 6.0, 7.0, 8.0]          # числа под столбцами моделей A01, P01, CS55, A13T
    # целые числа, не формулы; суммы столбцов не захватывают строку остатка
    assert not re.search(rf'<c r="[A-Z]+{rest_row}"[^>]*><f>', xml)
    for last in re.findall(r"<f>SUM\([A-Z]+\d+:[A-Z]+(\d+)\)</f>", xml):
        assert int(last) < total_row
    assert 'numFmtId="1"' in zipfile.ZipFile(io.BytesIO(book)).read("xl/styles.xml").decode("utf-8")


async def test_excel_row_dashes_without_link(app, anon, pool, monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)

    async def boom(plc_id, node_ids):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(rb, "read_poller_values", boom)
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    cells, _ = _sheet((await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content)
    _, rest_row = _find(cells, TITLE)
    assert _row_values(cells, rest_row) == [TITLE, "—", "—", "—", "—"]


async def test_excel_no_row_for_past_day_or_unbound_screen(app, anon, pool, live_today):
    _, scr = await _screen(pool, bound=True)
    past = date(2026, 10, 4)
    await _hourly(pool, scr, past)
    cells, _ = _sheet((await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": past.isoformat()})).content)
    assert TITLE not in cells.values()
    _, plain = await _screen(pool, "FL OTHER", bound=False)
    await _hourly(pool, plain)
    cells, _ = _sheet((await anon.get(f"/api/kpi/screens/{plain}/day.xlsx", params={"date": DAY.isoformat()})).content)
    assert TITLE not in cells.values()


async def test_excel_row_with_not_described_downtime(app, anon, pool, live_today):
    """Расчёт «не описано» в build_day_data не должен затирать значения остатка (ошибка с общим именем переменной)."""
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    await pool.execute(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 4, 3, 60, 20)", scr, DAY)
    cells, _ = _sheet((await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content)
    _, rest_row = _find(cells, TITLE)
    assert _row_values(cells, rest_row)[:3] == [TITLE, 5.0, 6.0]


async def test_autosave_report_has_row(app, pool, live_today):
    from combined import build_combined_report
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    book = (await build_combined_report(pool, scr, DAY))[0]
    cells, _ = _sheet(book)
    _, rest_row = _find(cells, TITLE)
    assert _row_values(cells, rest_row)[:3] == [TITLE, 5.0, 6.0]


async def test_day_preview_returns_rest(app, anon, pool, live_today):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert pv["rest"] == {"title": TITLE, "values": {"A01": 5, "P01": 6, "CS55": 7, "A13T": 8}}


# ---------- экран Andon соответствует файлу ----------

def _html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_andon_row_under_total_with_title_and_models():
    fl = _html()
    fl = fl[fl.index("function buildFlLayout"):fl.index("function openPlanDialog")]
    assert fl.index("Итого за смену") < fl.index(f">{TITLE}</td>")
    row = re.search(r'<tr class="rest-row"[^>]*>', fl)
    assert row and f'title="{HINT}"' in row.group(0)
    assert fl.index('class="rest-row"') > fl.index("<tfoot>") and fl.index('class="rest-row"') < fl.index("</tfoot>")
    assert 'data-k="rest_${m}"' in fl and "rest_A01" in fl           # только у экранов с привязками
    models = re.search(r"const FL_MODELS = (\[[^\]]+\])", _html()).group(1)
    assert json.loads(models.replace("'", '"')) == list(rb.REST_FIELDS)


def test_andon_paint_rest_dash_without_value_and_no_link_dash():
    html = _html()
    paint = html[html.index("function paintFl"):html.index("function setAndonBadge")]
    assert "rest_" in paint and "Number.isInteger" in paint
    tick = html[html.index("andonFails++"):html.index("andonFails++") + 900]
    assert 'data-k^="rest_"' in tick                                  # две неудачи подряд: «—»


def test_archive_hides_row_for_past_days():
    arch = (STATIC / "archive.js").read_text(encoding="utf-8")
    assert ".rest-row" in arch and "hidden" in arch


def test_row_style_has_no_warn_colors():
    html = _html()
    css = "\n".join(l for l in html.splitlines() if "rest-row" in l and "{" in l)
    assert css and "--warn" not in css and "#" not in css            # цвета только из переменных
