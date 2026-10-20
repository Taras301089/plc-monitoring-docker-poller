"""Столбец «Простой RB262» (голодание ФЛ): расчёт минут, запись поллером, API, экран Andon и Excel."""
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

import starve  # noqa: E402

DAY = date(2026, 10, 5)
HINT = "Подсчитывается автоматически. Голодание. Счётчик запускается при отсутствии кузовов в накопителе (буфере) перед ФЛ"


# ---------- расчёт ----------

def test_minutes_from_ms():
    assert starve.starve_minutes(1003578) == 16.73
    assert starve.starve_minutes(0) == 0
    assert starve.starve_minutes(None) is None
    assert starve.starve_minutes(-5) == 0


def test_mmss_label():
    assert starve.starve_mmss(1003578) == "16:43"
    assert starve.starve_mmss(59999) == "0:59"
    assert starve.starve_mmss(3_725_000) == "1:02:05"
    assert starve.starve_mmss(None) == "—"


def test_total_is_sum_of_intervals():
    assert starve.starve_total_ms([1000, None, 2000]) == 3000
    assert starve.starve_total_ms([None, None]) is None


def test_node_ids_are_zero_based_et_fields():
    assert starve.starve_node_id(0) == 'ns=3;s="DB_AMMKZ_Andon"."DownTimer"[0]."ET"'
    assert starve.starve_node_id(12).endswith('[12]."ET"')


# ---------- данные ----------

async def _screen(pool, name="FL RB262", starve_bound=True, template="fl_counter"):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://127.0.0.1:4840') RETURNING id", f"t_{name}")
    b = {"prod": "ns=3;s=\"P\"", "starts": "ns=3;s=\"S\""}
    if starve_bound:
        b.update({f"dt_{i}": starve.starve_node_id(i) for i in range(13)})
    scr = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        name, plc, template, json.dumps(b))
    return plc, scr


async def _hourly(pool, scr, ms=(1003578, 120000, None)):
    for i, m in enumerate(ms, start=1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact, starve_ms) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            scr, DAY, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), 4, 3, m)


# ---------- миграция привязки ----------

async def test_migration_adds_binding_only_when_plc_knows_the_array(app, pool):
    plc, scr = await _screen(pool, starve_bound=False)
    assert await starve.ensure_starve_binding(pool) == 0     # ПЛК ещё не знает массив
    await pool.execute(
        "INSERT INTO plc_discovered_nodes (plc_id, db_name, variable_name, node_id, browse_path) VALUES ($1, 'DB_AMMKZ_Andon', 'DownTimer', $2, 'x')",
        plc, 'ns=3;s="DB_AMMKZ_Andon"."DownTimer"')
    assert await starve.ensure_starve_binding(pool) == 1
    b = json.loads(await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr))
    assert b["dt_0"] == starve.starve_node_id(0) and b["dt_12"] == starve.starve_node_id(12) and b["prod"]
    assert await starve.ensure_starve_binding(pool) == 0     # повтор ничего не меняет


# ---------- поллер ----------

class FakeClient:
    def __init__(self, values=None, fail=False):
        self.values, self.fail = values or {}, fail

    async def read_values(self, node_ids):
        if self.fail:
            raise ConnectionError("нет связи")
        return {n: self.values[n] if n in self.values else {"ok": False, "v": None} for n in node_ids}


def _vals(scr_bindings, et):
    v = {scr_bindings["prod"]: {"ok": True, "v": [3, 4, 5]}, scr_bindings["starts"]: {"ok": True, "v": [480, 540, 600]}}
    for i, ms in enumerate(et):
        v[starve.starve_node_id(i)] = {"ok": True, "v": ms} if ms is not None else {"ok": False, "v": None}
    return v


async def _cycle(pool, plc, values, fail=False):
    import asyncio
    from kpi_collector import KpiCollector
    c = KpiCollector(lambda: pool, lambda: {plc: FakeClient(values, fail)}, asyncio.Event())
    await c._cycle()


async def _stored(pool, scr):
    return {r["idx"]: r["starve_ms"] for r in await pool.fetch("SELECT idx, starve_ms FROM kpi_hourly WHERE screen_id = $1", scr)}


async def test_poller_stores_last_et_per_interval_by_index(app, pool):
    plc, scr = await _screen(pool)
    b = json.loads(await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr))
    await _cycle(pool, plc, _vals(b, [1003578, 5000, 0]))
    assert await _stored(pool, scr) == {1: 1003578, 2: 5000, 3: 0}
    await _cycle(pool, plc, _vals(b, [1003578, 65000, 0]))       # то же значение таймера не суммируется, а заменяется
    assert (await _stored(pool, scr))[2] == 65000
    await _cycle(pool, plc, _vals(b, [1003578, 65000, 0]))
    assert (await _stored(pool, scr))[1] == 1003578


async def test_poller_keeps_old_values_without_connection(app, pool):
    plc, scr = await _screen(pool)
    b = json.loads(await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr))
    await _cycle(pool, plc, _vals(b, [1003578, 5000, 7000]))
    await _cycle(pool, plc, {}, fail=True)                       # ПЛК недоступен
    await _cycle(pool, plc, _vals(b, [None, None, None]))        # узлы прочитаны с ошибкой
    await _cycle(pool, plc, _vals(b, [0, 0, 0]))                 # сброс массива в ПЛК не затирает накопленное
    assert await _stored(pool, scr) == {1: 1003578, 2: 5000, 3: 7000}


async def test_poller_without_binding_leaves_column_empty(app, pool):
    plc, scr = await _screen(pool, name="FL OTHER", starve_bound=False)
    b = json.loads(await pool.fetchval("SELECT bindings FROM kpi_screens WHERE id = $1", scr))
    await _cycle(pool, plc, _vals(b, []))
    assert await _stored(pool, scr) == {1: None, 2: None, 3: None}


# ---------- API ----------

async def test_hourly_api_returns_ms_and_minutes(app, anon, pool):
    _, scr = await _screen(pool)
    await _hourly(pool, scr)
    d = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})).json()
    assert [(x["starve_ms"], x["starve_min"]) for x in d["intervals"]] == [(1003578, 16.73), (120000, 2.0), (None, None)]


async def test_day_preview_column_only_for_bound_screen(app, anon, pool):
    _, scr = await _screen(pool)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    h = pv["headers"]
    assert h[h.index("Takt Time") + 1] == "Простой RB262" and h[h.index("Takt Time") + 2] == "Отставание мин"
    col = h.index("Простой RB262")
    assert [r[col] for r in pv["rows"]] == [16.73, 2.0, "—"]
    _, other = await _screen(pool, name="GWM", starve_bound=False, template="body_counter")
    await _hourly(pool, other)
    pv2 = (await anon.get(f"/api/kpi/screens/{other}/day-preview", params={"date": DAY.isoformat()})).json()
    assert "Простой RB262" not in pv2["headers"]


# ---------- Excel ----------

def _sheet_cells(book):
    z = zipfile.ZipFile(io.BytesIO(book))
    shared = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells = {}
    for ref, attrs, body in re.findall(r'<c r="([A-Z]+\d+)"([^>/]*)>(.*?)</c>', xml, re.S):
        v = re.search(r"<v>(.*?)</v>", body)
        if v:
            cells[ref] = shared[int(v.group(1))] if 't="s"' in attrs else float(v.group(1))
    return cells, xml, z


async def test_excel_has_column_sum_and_hint(app, anon, pool):
    _, scr = await _screen(pool)
    await _hourly(pool, scr)
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content
    cells, xml, z = _sheet_cells(book)
    head = next(ref for ref, v in cells.items() if v == "Простой RB262")
    col, row = re.match(r"([A-Z]+)(\d+)", head).groups()
    assert [cells.get(f"{col}{int(row) + k}") for k in (1, 2)] == [16.73, 2.0]
    assert cells.get(f"{col}{int(row) + 3}") == "—"
    assert re.search(rf"<f>SUM\({col}{int(row) + 1}:{col}{int(row) + 3}\)</f>", xml)
    total = next(float(m) for m in re.findall(rf'<c r="{col}{int(row) + 4}"[^>]*><f>[^<]*</f><v>([^<]*)</v>', xml))
    assert total == 18.73
    assert HINT in " ".join(z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".xml") and ("comments" in n or "sharedStrings" in n))
    assert 'numFmtId="2"' in z.read("xl/styles.xml").decode("utf-8") or "0.00" in z.read("xl/styles.xml").decode("utf-8")


async def test_excel_without_binding_has_no_column(app, anon, pool):
    _, scr = await _screen(pool, name="GWM", starve_bound=False, template="body_counter")
    await _hourly(pool, scr)
    book = (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})).content
    cells, _, _ = _sheet_cells(book)
    assert "Простой RB262" not in cells.values() and HINT not in cells.values()


# ---------- экран Andon соответствует файлу ----------

def test_andon_screen_column_matches_excel():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert starve.STARVE_TITLE == "Простой RB262" and starve.STARVE_HINT == HINT
    fl = html[html.index("function buildFlLayout"):html.index("function openPlanDialog")]
    th = re.search(r'<th title="([^"]+)"[^>]*>Простой RB262</th>', fl)
    assert th and th.group(1) == HINT
    assert fl.index("Takt Time</th>") < fl.index("Простой RB262</th>") < fl.index("Отставание, мин</th>")
    assert 'data-k="starve"' in fl and 'data-k="starveSum"' in fl
    assert "dt_0" in fl   # столбец только для экранов с привязкой
    arch = (STATIC / "archive.js").read_text(encoding="utf-8")
    assert "starve_ms" in arch
    paint = html[html.index("function paintFl"):html.index("function setAndonBadge")]
    assert "dt_" in paint and "starveFmt" in paint


def test_screen_format_equals_server_format():
    """Подпись «М:СС» на экране (starveFmt) и на сервере (starve_mmss) совпадают."""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("нужен node")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    pad = re.search(r"const pad2 = [^\n]+", html).group(0)
    mm = re.search(r"const mmss = [^\n]+", html).group(0)
    fmt = re.search(r"const starveFmt = [^\n]+", html).group(0)
    samples = [0, 999, 59999, 1003578, 3599999, 3_725_000, None]
    code = f"{pad}\n{mm}\n{fmt}\nconsole.log(JSON.stringify({json.dumps(samples)}.map(starveFmt)))"
    out = json.loads(subprocess.run(["node", "-e", code], capture_output=True, timeout=30, check=True).stdout.decode("utf-8"))
    assert out == [starve.starve_mmss(x) for x in samples]
