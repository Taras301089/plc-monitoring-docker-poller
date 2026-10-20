"""Экран «FL RB294»: столбцы «Рем. зона» и «Простой BDC» и значение STR (прямой сход) в строке «Остаток кузовов на ФЛ»:
расчёт, привязки, запись поллером, API, экран Andon и Excel."""
import asyncio
import io
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
sys.path.insert(0, str(ROOT / "python-async-poller"))
sys.path.insert(0, str(ROOT / "fastapi-api"))

import pytest  # noqa: E402

import repair  # noqa: E402
import rest_bodies as rb  # noqa: E402
import starve  # noqa: E402

DAY = date(2026, 10, 5)
REPAIR_HINT = "Счётчик кузовов. Выход в ремонт"
BDC_HINT = "Подсчитывается автоматически. Время простоя BDC (DownTimer_BDC)"
STR_HINT = ("STR: прямой сход, отношение кузовов без дефектов к общему количеству кузовов. В течение дня рассчитывается автоматически "
            "(факт минус выход в ремонт, делённое на факт), итог дня берётся из ПЛК (RepairPercent, считается в конце дня)")


# ---------- расчёт ----------

def test_titles_and_hints():
    assert repair.REPAIR_TITLE == "Рем. зона" and repair.REPAIR_HINT == REPAIR_HINT
    assert repair.BDC_TITLE == "Простой BDC" and repair.BDC_HINT == BDC_HINT
    assert repair.STR_TITLE == "STR" and repair.STR_HINT == STR_HINT


def test_node_ids_interval_k_is_element_k():
    assert repair.repair_node_id(1) == 'ns=3;s="FB_AMMKZ_Andon_DB"."RepairCount"[1]'
    assert repair.repair_node_id(13) == 'ns=3;s="FB_AMMKZ_Andon_DB"."RepairCount"[13]'
    assert repair.bdc_node_id(1) == 'ns=3;s="DB55_temporary_data_storage"."DownTimer_BDC"[1]."ET"'
    assert repair.bdc_node_id(13).endswith('[13]."ET"')
    assert repair.element_of_interval(1) == 1 and repair.element_of_interval(13) == 13   # [0] не интервал


def test_str_percent():
    assert repair.str_percent(100, 5) == 95.0
    assert repair.str_percent(3, 1) == 66.67
    assert repair.str_percent(10, 0) == 100.0
    assert repair.str_percent(32, 3) == 90.63          # ровно 90,625: округление от половины вверх, как на экране
    assert repair.str_percent(0, 0) is None            # факт 0 -> нет значения
    assert repair.str_percent(None, 1) is None
    assert repair.str_percent(10, None) is None


def test_str_label_format():
    assert repair.str_label(95.0) == "95,00 %"
    assert repair.str_label(66.67) == "66,67 %"
    assert repair.str_label(None) == "—"


def test_repair_value_and_total():
    assert repair.repair_value(3) == 3 and repair.repair_value(-1) == 0
    assert repair.repair_value(None) is None and repair.repair_value(True) is None
    assert repair.repair_total([1, None, 2]) == 3
    assert repair.repair_total([None, None]) is None


def test_bdc_uses_common_ms_functions():
    assert repair.bdc_minutes(120000) == starve.starve_minutes(120000) == 2.0
    assert repair.bdc_mmss(1003578) == starve.starve_mmss(1003578) == "16:43"


# ---------- привязки: только «FL RB294» ----------

async def _screen(pool, name="FL RB294", bound=False, template="fl_counter", plc=None):
    if plc is None:
        plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://127.0.0.1:4840') RETURNING id", f"rb_{name}")
    b = {"prod": 'ns=3;s="P"', "starts": 'ns=3;s="S"'}
    if bound:
        b.update(repair.bindings_for())
    scr = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        name, plc, template, json.dumps(b))
    return plc, scr


async def _bindings(pool, scr):
    raw = await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr)
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def test_migration_only_for_fl_rb294(app, pool):
    plc, s294 = await _screen(pool, "FL RB294")
    _, s262 = await _screen(pool, "FL RB262", plc=plc)
    _, other = await _screen(pool, "GWM", template="body_counter", plc=plc)
    assert await repair.ensure_repair_binding(pool) == 1          # без проверки по таблице узлов
    b = await _bindings(pool, s294)
    assert b["rc_1"] == repair.repair_node_id(1) and b["rc_13"] == repair.repair_node_id(13) and b["prod"]
    assert b["bd_1"] == repair.bdc_node_id(1) and b["bd_13"] == repair.bdc_node_id(13)
    assert "rc_0" not in b and "bd_0" not in b
    for scr in (s262, other):
        assert not repair.has_repair(await _bindings(pool, scr))
    assert await repair.ensure_repair_binding(pool) == 0          # повтор ничего не меняет


async def test_schema_has_nullable_columns(app, pool):
    rows = await pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'kpi_hourly' "
        "AND column_name IN ('repair_cnt', 'bdc_ms')")
    got = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert got == {"repair_cnt": ("integer", "YES"), "bdc_ms": ("bigint", "YES")}


# ---------- поллер: интервал k = элемент [k] ----------

class FakeClient:
    def __init__(self, values=None, fail=False):
        self.values, self.fail = values or {}, fail

    async def read_values(self, node_ids):
        if self.fail:
            raise ConnectionError("нет связи")
        return {n: self.values[n] if n in self.values else {"ok": False, "v": None} for n in node_ids}


def _vals(b, rc, bd):
    """rc, bd: значения элементов [1], [2], ... (по интервалам); None = ошибка чтения."""
    v = {b["prod"]: {"ok": True, "v": [3, 4, 5]}, b["starts"]: {"ok": True, "v": [480, 540, 600]}}
    for k, x in enumerate(rc, start=1):
        v[repair.repair_node_id(k)] = {"ok": True, "v": x} if x is not None else {"ok": False, "v": None}
    for k, x in enumerate(bd, start=1):
        v[repair.bdc_node_id(k)] = {"ok": True, "v": x} if x is not None else {"ok": False, "v": None}
    return v


async def _cycle(pool, plc, values, fail=False):
    from kpi_collector import KpiCollector
    c = KpiCollector(lambda: pool, lambda: {plc: FakeClient(values, fail)}, asyncio.Event())
    await c._cycle()


async def _stored(pool, scr):
    rows = await pool.fetch("SELECT idx, repair_cnt, bdc_ms FROM kpi_hourly WHERE screen_id = $1", scr)
    return {r["idx"]: (r["repair_cnt"], r["bdc_ms"]) for r in rows}


async def test_poller_interval_k_is_element_k(app, pool):
    plc, scr = await _screen(pool, bound=True)
    b = await _bindings(pool, scr)
    await _cycle(pool, plc, _vals(b, [1, 0, 2], [1003578, 5000, 0]))
    assert await _stored(pool, scr) == {1: (1, 1003578), 2: (0, 5000), 3: (2, 0)}
    await _cycle(pool, plc, _vals(b, [1, 0, 2], [1003578, 65000, 0]))      # таймер заменяется последним значением
    assert (await _stored(pool, scr))[2] == (0, 65000)


async def test_poller_keeps_values_without_link_and_on_reset(app, pool):
    plc, scr = await _screen(pool, bound=True)
    b = await _bindings(pool, scr)
    await _cycle(pool, plc, _vals(b, [1, 2, 3], [1000, 2000, 3000]))
    await _cycle(pool, plc, {}, fail=True)                                  # ПЛК недоступен
    await _cycle(pool, plc, _vals(b, [None, None, None], [None, None, None]))   # ошибка чтения узлов
    await _cycle(pool, plc, _vals(b, [0, 0, 0], [0, 0, 0]))                 # сброс массивов в ПЛК не затирает накопленное
    assert await _stored(pool, scr) == {1: (1, 1000), 2: (2, 2000), 3: (3, 3000)}


async def test_poller_unbound_screen_leaves_columns_empty(app, pool):
    plc, scr = await _screen(pool, name="FL RB262", bound=False)
    b = await _bindings(pool, scr)
    await _cycle(pool, plc, _vals(b, [], []))
    assert await _stored(pool, scr) == {1: (None, None), 2: (None, None), 3: (None, None)}


# ---------- API ----------

async def _hourly(pool, scr, day=DAY, repair_cnt=(1, 0, 2), bdc=(1003578, 120000, None), fact=(3, 4, 5)):
    for i, (rc, ms, f) in enumerate(zip(repair_cnt, bdc, fact), start=1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact, repair_cnt, bdc_ms) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), 4, f, rc, ms)


async def test_hourly_api_returns_values_and_str(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    d = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})).json()
    assert [(x["repair_cnt"], x["bdc_ms"], x["bdc_min"]) for x in d["intervals"]] == [(1, 1003578, 16.73), (0, 120000, 2.0), (2, None, None)]
    assert d["str"] == {"fact": 12, "repair": 3, "pct": 75.0, "source": "calc", "plc": None}


async def test_hourly_api_str_dash_without_repair_data(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr, repair_cnt=(None, None, None))
    d = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})).json()
    assert d["str"]["pct"] is None and d["str"]["repair"] is None


async def test_day_preview_columns_order_and_values(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    h = pv["headers"]
    t = h.index("Takt Time")
    assert h[t + 1:t + 4] == ["Рем. зона", "Простой BDC", "Отставание мин"]
    assert [r[h.index("Рем. зона")] for r in pv["rows"]] == [1, 0, 2]
    assert [r[h.index("Простой BDC")] for r in pv["rows"]] == [16.73, 2.0, "—"]
    assert "Простой RB262" not in h
    assert pv["str"] == {"fact": 12, "repair": 3, "pct": 75.0, "source": "calc", "plc": None}


async def test_other_screens_have_no_new_columns(app, anon, pool):
    _, scr = await _screen(pool, "FL RB262", bound=False)
    await _hourly(pool, scr, repair_cnt=(None, None, None), bdc=(None, None, None))
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert "Рем. зона" not in pv["headers"] and "Простой BDC" not in pv["headers"] and pv.get("str") is None


# ---------- Excel ----------

def _sheet(book):
    z = zipfile.ZipFile(io.BytesIO(book))
    shared = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells = {}
    for ref, attrs, body in re.findall(r'<c r="([A-Z]+\d+)"([^>/]*)>(.*?)</c>', xml, re.S):
        v = re.search(r"<v>(.*?)</v>", body)
        if v:
            cells[ref] = shared[int(v.group(1))] if 't="s"' in attrs else v.group(1) if 't="str"' in attrs else float(v.group(1))
    return cells, xml, z


def _pos(ref):
    c, r = re.match(r"([A-Z]+)(\d+)", ref).groups()
    return c, int(r)


def _find(cells, text):
    return _pos(next(r for r, v in cells.items() if v == text))


async def _xlsx(anon, scr, day=DAY):
    return (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": day.isoformat()})).content


async def test_excel_columns_sum_and_str(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)                                   # прошлые данные: STR считается по базе и показывается
    cells, xml, z = _sheet(await _xlsx(anon, scr))
    rc_col, head = _find(cells, "Рем. зона")
    bd_col, head2 = _find(cells, "Простой BDC")
    assert head == head2
    assert [cells.get(f"{rc_col}{head + k}") for k in (1, 2, 3)] == [1.0, 0.0, 2.0]
    assert [cells.get(f"{bd_col}{head + k}") for k in (1, 2)] == [16.73, 2.0] and cells.get(f"{bd_col}{head + 3}") == "—"
    assert re.search(rf"<f>SUM\({rc_col}{head + 1}:{rc_col}{head + 3}\)</f>", xml)
    assert re.search(rf"<f>SUM\({bd_col}{head + 1}:{bd_col}{head + 3}\)</f>", xml)
    _, total_row = _find(cells, "Итого")
    assert cells[f"{rc_col}{total_row}"] == 3.0 and cells[f"{bd_col}{total_row}"] == 18.73
    # STR: подпись и формула со ссылками на ячейки «Итого»; значение равно экранному (75,00 %)
    lbl_col, str_row = _find(cells, "STR:")
    assert str_row == total_row + 1
    val_col = chr(ord(lbl_col) + 1)
    fact_col = next(_pos(r)[0] for r, v in cells.items() if v == "Факт" and _pos(r)[1] == head)
    m = re.search(rf'<c r="{val_col}{str_row}"[^>]*><f>([^<]*)</f><v>([^<]*)</v>', xml)
    assert m and float(m.group(2)) == 0.75
    assert f"{fact_col}{total_row}" in m.group(1).replace("$", "") and f"{rc_col}{total_row}" in m.group(1).replace("$", "")
    assert repair.str_percent(12, 3) / 100 == float(m.group(2))
    assert '0.00%' in z.read("xl/styles.xml").decode("utf-8")
    texts = " ".join(z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".xml") and ("comments" in n or "sharedStrings" in n))
    for hint in (REPAIR_HINT, BDC_HINT, STR_HINT):
        assert hint in texts


async def test_excel_dashes_without_data(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr, repair_cnt=(None, None, None), bdc=(None, None, None))
    cells, xml, _ = _sheet(await _xlsx(anon, scr))
    rc_col, head = _find(cells, "Рем. зона")
    assert [cells.get(f"{rc_col}{head + k}") for k in (1, 2, 3)] == ["—", "—", "—"]
    lbl_col, str_row = _find(cells, "STR:")
    val_col = chr(ord(lbl_col) + 1)
    m = re.search(rf'<c r="{val_col}{str_row}"[^>]*t="str"[^>]*><f>[^<]*</f><v>([^<]*)</v>', xml)
    assert m and m.group(1) == "—"


async def test_excel_str_shares_row_with_rest_row(app, anon, pool, monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)

    async def fake(plc_id, node_ids):
        return {n: {"ok": True, "v": i + 5} for i, n in enumerate(node_ids)}

    monkeypatch.setattr(rb, "read_poller_values", fake)
    _, scr = await _screen(pool, bound=True)
    b = await _bindings(pool, scr)
    b.update({f"rest_{m}": rb.rest_node_id(m) for m in rb.REST_FIELDS})
    await pool.execute("UPDATE kpi_screens SET bindings = $2::jsonb WHERE id = $1", scr, json.dumps(b))
    await _hourly(pool, scr)
    cells, _, _ = _sheet(await _xlsx(anon, scr))
    _, rest_row = _find(cells, "Остаток кузовов на ФЛ")
    _, str_row = _find(cells, "STR:")
    assert str_row == rest_row                                  # STR справа в строке «Остаток», отдельной строки нет


async def test_excel_other_screen_has_no_columns_or_str(app, anon, pool):
    _, scr = await _screen(pool, "FL RB262", bound=False)
    await _hourly(pool, scr, repair_cnt=(None, None, None), bdc=(None, None, None))
    cells, _, z = _sheet(await _xlsx(anon, scr))
    assert "Рем. зона" not in cells.values() and "Простой BDC" not in cells.values() and "STR:" not in cells.values()


# ---------- экран Andon соответствует файлу ----------

def test_andon_screen_matches_excel():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    fl = html[html.index("function buildFlLayout"):html.index("function openPlanDialog")]
    assert re.search(r'<th title="Счётчик кузовов\. Выход в ремонт"[^>]*>Рем\. зона</th>', fl)
    assert re.search(r'<th title="Подсчитывается автоматически\. Время простоя BDC \(DownTimer_BDC\)"[^>]*>Простой BDC</th>', fl)
    assert fl.index("Takt Time</th>") < fl.index("Рем. зона</th>") < fl.index("Простой BDC</th>") < fl.index("Отставание, мин</th>")
    for k in ('data-k="repair"', 'data-k="bdc"', 'data-k="repairSum"', 'data-k="bdcSum"', 'data-k="str"'):
        assert k in fl
    assert "STR:" in fl and repair.STR_HINT in fl and "rc_1" in fl       # столбцы только у экранов с привязкой
    # STR стоит в строке остатка кузовов, справа от значений моделей
    row = fl[fl.index('class="rest-row"'):]
    assert row.index("Остаток кузовов на ФЛ") < row.index('data-k="str"')
    paint = html[html.index("function paintFl"):html.index("function setAndonBadge")]
    for k in ("rc_", "bd_", "strPick", "strFmt", "repairSum", "bdcSum"):
        assert k in paint
    arch = (STATIC / "archive.js").read_text(encoding="utf-8")
    assert "repair_cnt" in arch and "bdc_ms" in arch and "str" in arch and "tr.rest-row" in arch


def test_no_link_dashes_listed_in_live_screen():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    i = html.index("andonFails >= 2")
    chunk = html[i:i + 700]
    for k in ('[data-k="repair"]', '[data-k="bdc"]', '[data-k="repairSum"]', '[data-k="bdcSum"]', '[data-k="str"]'):
        assert k in chunk


def test_screen_str_formula_equals_server():
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("нужен node")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    fn = re.search(r"const strPct = [^\n]+", html).group(0)
    fmt = re.search(r"const strFmt = [^\n]+", html).group(0)
    cases = [[100, 5], [3, 1], [10, 0], [0, 0], [None, 1], [10, None], [12, 3], [32, 3], [16, 1], [7, 9]]
    code = f"{fn}\n{fmt}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([f, r]) => strFmt(strPct(f, r)))))"
    out = json.loads(subprocess.run(["node", "-e", code], capture_output=True, timeout=30, check=True).stdout.decode("utf-8"))
    assert out == [repair.str_label(repair.str_percent(f, r)) for f, r in cases]
