"""STR (прямой сход) экрана «FL RB294»: в течение дня расчётное значение, итог дня берётся из ПЛК (FB_AMMKZ_Andon_DB.RepairPercent).
Выбор значения одной серверной функцией, привязка rpct, сохранение поллером, API, архив, Excel (число или формула), журнал расхождения."""
import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
sys.path.insert(0, str(ROOT / "python-async-poller"))
sys.path.insert(0, str(ROOT / "fastapi-api"))

import pytest  # noqa: E402

import repair  # noqa: E402
from test_repair_bdc_columns import (FakeClient, _bindings, _find, _hourly, _screen, _sheet, _stored, _vals, _xlsx, DAY)  # noqa: E402,F401

NEW_HINT = ("STR: прямой сход, отношение кузовов без дефектов к общему количеству кузовов. В течение дня рассчитывается автоматически "
            "(факт минус выход в ремонт, делённое на факт), итог дня берётся из ПЛК (RepairPercent, считается в конце дня)")


# ---------- выбор значения ----------

def test_hint_text():
    assert repair.STR_HINT == NEW_HINT


def test_rpct_node_and_binding():
    assert repair.RPCT_KEY == "rpct"
    assert repair.rpct_node_id() == 'ns=3;s="FB_AMMKZ_Andon_DB"."RepairPercent"'
    assert repair.bindings_for()["rpct"] == repair.rpct_node_id()


def test_plc_str_value():
    assert repair.plc_str_value(97.73) == 97.73
    assert repair.plc_str_value(97.7300033569) == 97.73       # Float32 из ПЛК
    assert repair.plc_str_value(100) == 100.0
    for bad in (0, 0.0, -1.5, None, True, "97", float("nan"), float("inf"), 100.5):
        assert repair.plc_str_value(bad) is None


def test_pick_str_plc_wins_when_positive():
    assert repair.pick_str(100, 5, 97.73) == (97.73, "plc")
    assert repair.pick_str(100, 5, 0.0) == (95.0, "calc")        # в течение дня ПЛК отдаёт 0
    assert repair.pick_str(100, 5, None) == (95.0, "calc")
    assert repair.pick_str(0, 0, None) == (None, "calc")
    assert repair.pick_str(None, None, 91.5) == (91.5, "plc")    # итог ПЛК важнее отсутствия факта


def test_str_summary_picks_plc_from_rows():
    rows = [{"plan": 4, "fact": 10, "repair_cnt": 1, "repair_pct_plc": None},
            {"plan": 4, "fact": 10, "repair_cnt": 1, "repair_pct_plc": 88.5}]
    s = repair.str_summary(rows)
    assert s == {"fact": 20, "repair": 2, "pct": 88.5, "source": "plc", "plc": 88.5}
    rows[1]["repair_pct_plc"] = None
    assert repair.str_summary(rows) == {"fact": 20, "repair": 2, "pct": 90.0, "source": "calc", "plc": None}


def test_mismatch_logged_once(caplog):
    repair._WARNED.clear()
    rows = [{"plan": 4, "fact": 100, "repair_cnt": 5, "repair_pct_plc": 90.0}]
    with caplog.at_level(logging.WARNING, logger="repair"):
        repair.str_summary(rows, key=("t", 1))
        repair.str_summary(rows, key=("t", 1))
    assert len([r for r in caplog.records if "STR" in r.getMessage()]) == 1
    caplog.clear()
    rows[0]["repair_pct_plc"] = 95.03                          # расхождение 0,03 п.п. не шумит
    with caplog.at_level(logging.WARNING, logger="repair"):
        repair.str_summary(rows, key=("t", 2))
    assert not caplog.records


# ---------- миграция ----------

async def test_schema_column(app, pool):
    r = await pool.fetchrow("SELECT data_type, is_nullable FROM information_schema.columns WHERE table_name = 'kpi_hourly' AND column_name = 'repair_pct_plc'")
    assert r is not None and (r["data_type"], r["is_nullable"]) == ("real", "YES")


async def test_binding_only_fl_rb294_and_added_to_bound_screen(app, pool):
    plc, s294 = await _screen(pool, "FL RB294")
    _, s262 = await _screen(pool, "FL RB262", plc=plc)
    _, other = await _screen(pool, "GWM", template="body_counter", plc=plc)
    assert await repair.ensure_repair_binding(pool) == 1
    assert (await _bindings(pool, s294))["rpct"] == repair.rpct_node_id()
    for scr in (s262, other):
        assert "rpct" not in await _bindings(pool, scr)
    # экран уже с «Рем. зона», но без rpct: ключ добавляется, остальное не трогается
    b = await _bindings(pool, s294)
    del b["rpct"]
    await pool.execute("UPDATE kpi_screens SET bindings = $2::jsonb WHERE id = $1", s294, json.dumps(b))
    assert await repair.ensure_repair_binding(pool) == 1
    assert (await _bindings(pool, s294))["rpct"] == repair.rpct_node_id()
    assert await repair.ensure_repair_binding(pool) == 0


# ---------- поллер ----------

async def _cyc(pool, plc, values, fail=False):
    from kpi_collector import KpiCollector
    import asyncio
    await KpiCollector(lambda: pool, lambda: {plc: FakeClient(values, fail)}, asyncio.Event())._cycle()


async def _pct(pool, scr, day=None):
    rows = await pool.fetch("SELECT prod_date, idx, repair_pct_plc FROM kpi_hourly WHERE screen_id = $1 ORDER BY prod_date, idx", scr)
    return {(r["prod_date"], r["idx"]): r["repair_pct_plc"] for r in rows if day is None or r["prod_date"] == day}


def _with_pct(b, pct, ok=True):
    v = _vals(b, [1, 2, 3], [0, 0, 0])
    v[b["rpct"]] = {"ok": ok, "v": pct}
    return v


async def test_poller_saves_positive_and_does_not_wipe(app, pool):
    plc, scr = await _screen(pool, bound=True)
    b = await _bindings(pool, scr)
    await _cyc(pool, plc, _with_pct(b, 0.0))                       # в течение дня 0: ничего не пишется
    assert set((await _pct(pool, scr)).values()) == {None}
    await _cyc(pool, plc, _with_pct(b, 97.73))
    got = await _pct(pool, scr)
    assert len(got) == 3 and {round(x, 2) for x in got.values()} == {97.73}
    await _cyc(pool, plc, _with_pct(b, 0.0))                       # ПЛК обнулил на новый день: итог не затирается
    await _cyc(pool, plc, {}, fail=True)                           # нет связи
    await _cyc(pool, plc, _with_pct(b, None, ok=False))            # ошибка чтения узла
    assert {round(x, 2) for x in (await _pct(pool, scr)).values()} == {97.73}
    await _cyc(pool, plc, _with_pct(b, 96.5))                      # пересчёт ПЛК в тот же день: последнее ненулевое
    assert {round(x, 2) for x in (await _pct(pool, scr)).values()} == {96.5}


async def test_poller_ignores_stale_value_of_previous_day(app, pool):
    plc, scr = await _screen(pool, bound=True)
    b = await _bindings(pool, scr)
    from kpi_collector import production_date
    today = production_date(480)
    yesterday = today - timedelta(days=1)
    await _hourly(pool, scr, day=yesterday)
    await pool.execute("UPDATE kpi_hourly SET repair_pct_plc = 97.73 WHERE screen_id = $1 AND prod_date = $2", scr, yesterday)
    await _cyc(pool, plc, _with_pct(b, 97.73))                     # ПЛК ещё держит вчерашнее число: сегодняшний день не получает его
    assert set((await _pct(pool, scr, today)).values()) == {None}
    await _cyc(pool, plc, _with_pct(b, 95.1))                      # новое число: пишется
    assert {round(x, 2) for x in (await _pct(pool, scr, today)).values()} == {95.1}
    assert {round(x, 2) for x in (await _pct(pool, scr, yesterday)).values()} == {97.73}


async def test_poller_unbound_screen_has_no_value(app, pool):
    plc, scr = await _screen(pool, "FL RB262", bound=False)
    b = await _bindings(pool, scr)
    await _cyc(pool, plc, _vals(b, [], []))
    assert set((await _pct(pool, scr)).values()) == {None}


# ---------- API, архив ----------

async def test_hourly_api_str_plc_for_past_day(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)                                       # факт 12, ремонт 3: расчёт 75,00
    await pool.execute("UPDATE kpi_hourly SET repair_pct_plc = 97.73 WHERE screen_id = $1 AND prod_date = $2", scr, DAY)
    d = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})).json()
    assert d["str"] == {"fact": 12, "repair": 3, "pct": 97.73, "source": "plc", "plc": 97.73}
    day2 = DAY + timedelta(days=1)
    await _hourly(pool, scr, day=day2)                             # без значения ПЛК: расчётное, как раньше
    d2 = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": day2.isoformat()})).json()
    assert d2["str"] == {"fact": 12, "repair": 3, "pct": 75.0, "source": "calc", "plc": None}


async def test_mismatch_goes_to_log_from_api(app, anon, pool, caplog):
    repair._WARNED.clear()
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    await pool.execute("UPDATE kpi_hourly SET repair_pct_plc = 97.73 WHERE screen_id = $1 AND prod_date = $2", scr, DAY)
    with caplog.at_level(logging.WARNING, logger="repair"):
        for _ in range(3):
            await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})
    assert len([r for r in caplog.records if "STR" in r.getMessage()]) == 1


def test_archive_uses_server_value():
    arch = (STATIC / "archive.js").read_text(encoding="utf-8")
    assert "data.str.pct" in arch and "strFmt(p)" in arch          # архив показывает значение сервера, ничего не пересчитывает
    assert "strPct(" not in arch


# ---------- Excel: ПЛК -> число, иначе формула ----------

async def test_excel_plc_value_is_number_not_formula(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    await pool.execute("UPDATE kpi_hourly SET repair_pct_plc = 97.73 WHERE screen_id = $1 AND prod_date = $2", scr, DAY)
    cells, xml, z = _sheet(await _xlsx(anon, scr))
    lbl_col, str_row = _find(cells, "STR:")
    val_col = chr(ord(lbl_col) + 1)
    m = re.search(rf'<c r="{val_col}{str_row}"([^>]*)>(.*?)</c>', xml, re.S)
    assert m and "<f>" not in m.group(2)
    assert abs(float(re.search(r"<v>(.*?)</v>", m.group(2)).group(1)) - 0.9773) < 1e-9
    assert "0.00%" in z.read("xl/styles.xml").decode("utf-8")
    texts = " ".join(z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".xml") and ("comments" in n or "sharedStrings" in n))
    assert NEW_HINT in texts


async def test_excel_calc_value_is_still_formula(app, anon, pool):
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    cells, xml, _ = _sheet(await _xlsx(anon, scr))
    lbl_col, str_row = _find(cells, "STR:")
    val_col = chr(ord(lbl_col) + 1)
    assert re.search(rf'<c r="{val_col}{str_row}"[^>]*><f>[^<]+</f><v>0\.75</v>', xml)


async def test_autosave_file_has_same_str_as_screen(app, anon, pool):
    import io
    import zipfile
    from combined import build_combined_report
    _, scr = await _screen(pool, bound=True)
    await _hourly(pool, scr)
    await pool.execute("UPDATE kpi_hourly SET repair_pct_plc = 97.73 WHERE screen_id = $1 AND prod_date = $2", scr, DAY)
    data = (await build_combined_report(pool, scr, DAY))[0]
    cells, xml, _ = _sheet(data)
    lbl_col, str_row = _find(cells, "STR:")
    val_col = chr(ord(lbl_col) + 1)
    m = re.search(rf'<c r="{val_col}{str_row}"([^>]*)>(.*?)</c>', xml, re.S)
    assert m and "<f>" not in m.group(2) and abs(float(re.search(r"<v>(.*?)</v>", m.group(2)).group(1)) - 0.9773) < 1e-9
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert pv["str"]["pct"] == 97.73 and pv["str"]["source"] == "plc"


# ---------- экран Andon ----------

def test_live_screen_reads_rpct_and_hint_matches():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    fl = html[html.index("function buildFlLayout"):html.index("function openPlanDialog")]
    assert repair.STR_HINT in fl
    paint = html[html.index("function paintFl"):html.index("function setAndonBadge")]
    assert "rpct" in paint and "strPick" in paint


def test_screen_pick_equals_server():
    if shutil.which("node") is None:
        pytest.skip("нужен node")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    pct = re.search(r"const strPct = [^\n]+", html).group(0)
    fmt = re.search(r"const strFmt = [^\n]+", html).group(0)
    pick = re.search(r"const strPick = [^\n]+", html).group(0)
    cases = [[97.73, 100, 5], [0, 100, 5], [None, 100, 5], [97.7300033569, 3, 1], [-1, 10, 0], [101, 10, 1], [95.5, 0, 0], [0, 0, 0], [None, None, None]]
    code = f"{pct}\n{fmt}\n{pick}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([p, f, r]) => strFmt(strPick(p, f, r)))))"
    out = json.loads(subprocess.run(["node", "-e", code], capture_output=True, timeout=30, check=True).stdout.decode("utf-8"))
    exp = []
    for p, f, r in cases:
        v, _ = repair.pick_str(f, r, p)
        exp.append(repair.str_label(v))
    assert out == exp
