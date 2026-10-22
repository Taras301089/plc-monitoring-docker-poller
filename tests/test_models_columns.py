"""Столбцы моделей (A01, P01, CS55, A13T) таблицы интервалов: экран Andon (Finish Line), предпросмотр, Excel дня и автосохранение."""
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

import flmodels  # noqa: E402
import history  # noqa: E402
import rest_bodies as rb  # noqa: E402

MODELS = ["A01", "P01", "CS55", "A13T"]
DAY = date(2026, 10, 5)
ARR = {"A01": [1, 2, 0], "P01": [0, 1, 1], "CS55": [2, 0, 0], "A13T": [0, 0, 2]}
SUMS = {"A01": 3, "P01": 2, "CS55": 2, "A13T": 2}
REST = {"A01": 5, "P01": 6, "CS55": 7, "A13T": 8}
REST_TITLE = "Остаток кузовов на ФЛ"


def _mnode(m):
    return f'ns=3;s="Hourly"."{m}"'


# ---------- единая функция значений ----------

def test_models_order_and_hint():
    assert flmodels.MODELS == MODELS
    assert flmodels.model_hint("A01") == "Количество кузовов модели A01 за интервал"
    assert flmodels.has_models("fl_counter") and not flmodels.has_models("body_counter") and not flmodels.has_models("manual_counter")


def test_interval_value_by_idx_and_dash():
    c = {"A01": [4, 5], "P01": None}
    assert flmodels.interval_value(c, "A01", 1) == 4 and flmodels.interval_value(c, "A01", 2) == 5
    assert flmodels.interval_value(c, "A01", 3) == 0          # массив короче: как на экране «0»
    assert flmodels.interval_value(c, "P01", 1) == "—"          # нет данных


def test_counts_from_values():
    b = {f"m_{m}": _mnode(m) for m in MODELS}
    v = {b["m_A01"]: {"ok": True, "v": [1, 2]}, b["m_P01"]: {"ok": False, "v": None}, b["m_CS55"]: {"ok": True, "v": 7}}
    assert flmodels.counts_from_values(b, v) == {"A01": [1, 2], "P01": None, "CS55": None, "A13T": None}


async def test_fetch_counts_only_today(monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)
    calls = []

    async def fake(plc_id, nodes):
        calls.append(nodes)
        return {n: {"ok": True, "v": [9]} for n in nodes}

    monkeypatch.setattr(rb, "read_poller_values", fake)
    b = {f"m_{m}": _mnode(m) for m in MODELS}
    assert await flmodels.fetch_counts(1, b, DAY) == {m: [9] for m in MODELS}
    assert await flmodels.fetch_counts(1, b, date(2026, 10, 4)) == {m: None for m in MODELS}
    assert await flmodels.fetch_counts(1, {}, DAY) == {m: None for m in MODELS}
    assert len(calls) == 1


# ---------- данные ----------

@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)
    mnodes = {_mnode(m): m for m in MODELS}

    async def fake(plc_id, nodes):
        out = {}
        for i, n in enumerate(nodes):
            out[n] = {"ok": True, "v": ARR[mnodes[n]]} if n in mnodes else {"ok": True, "v": 5 + i}
        return out

    monkeypatch.setattr(rb, "read_poller_values", fake)


async def _screen(pool, name="FL RB262", template="fl_counter", models=True, starve=False):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://127.0.0.1:4840') RETURNING id", f"mc_{name}")
    b = {"prod": 'ns=3;s="P"', "starts": 'ns=3;s="S"'}
    if models:
        b.update({f"m_{m}": _mnode(m) for m in MODELS})
        b.update({f"rest_{m}": rb.rest_node_id(m) for m in MODELS})
    if starve:
        b.update({f"dt_{i}": f'ns=3;s="DT"[{i}]."ET"' for i in range(13)})
    return await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        name, plc, template, json.dumps(b))


async def _hourly(pool, scr, day=DAY):
    for i in (1, 2, 3):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact, takt_sec) VALUES ($1,$2,$3,$4,$5,4,3,600)",
            scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1))
    # в первом интервале две строки (описанная причина и «не описан»): числа интервала должны стоять только на первой
    dt = await pool.fetchval(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1,$2,1,4,3,60,20) RETURNING id", scr, day)
    reason = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_r', '', 9999) RETURNING id")
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, note) VALUES ($1, $2, 10, 'x')", dt, reason)


def _cells(book):
    z = zipfile.ZipFile(io.BytesIO(book))
    shared = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells, formulas = {}, {}
    for ref, attrs, body in re.findall(r'<c r="([A-Z]+\d+)"([^>/]*)>(.*?)</c>', xml, re.S):
        v = re.search(r"<v>(.*?)</v>", body)
        f = re.search(r"<f>(.*?)</f>", body)
        if f:
            formulas[ref] = f.group(1)
        if v:
            cells[ref] = shared[int(v.group(1))] if 't="s"' in attrs else float(v.group(1))
    return cells, formulas, z


def _letter(i):
    return chr(ord("A") + i)


def _table(cells, headers):
    """Находит строку заголовков; возвращает (номер строки шапки, {заголовок: буква столбца})."""
    row = int(re.match(r"[A-Z]+(\d+)", next(r for r, v in cells.items() if v == "Дата")).group(1))
    assert [cells.get(f"{_letter(i)}{row}") for i in range(len(headers))] == headers
    return row, {h: _letter(i) for i, h in enumerate(headers)}


async def _book(anon, scr, day=DAY):
    return (await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": day.isoformat()})).content


# ---------- предпросмотр и Excel ----------

async def test_preview_headers_models_between_delta_and_takt(app, anon, pool, live):
    scr = await _screen(pool)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    h = pv["headers"]
    assert h[h.index("±") + 1: h.index("Takt Time")] == MODELS          # порядок и место как на экране
    assert h == history.day_headers(False, True)
    col = {m: h.index(m) for m in MODELS}
    # строки: интервал 1 даёт две строки, значения модели только в первой
    assert [r[col["A01"]] for r in pv["rows"]] == [1, "", 2, 0]
    assert [r[col["A13T"]] for r in pv["rows"]] == [0, "", 0, 2]
    assert all(len(r) == len(h) for r in pv["rows"])


async def test_excel_headers_equal_preview_and_sums(app, anon, pool, live):
    scr = await _screen(pool)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    cells, formulas, z = _cells(await _book(anon, scr))
    row, L = _table(cells, pv["headers"])                              # заголовки файла = заголовки предпросмотра
    n = len(pv["rows"])
    first, last, total = row + 1, row + n, row + n + 1
    assert cells[f"A{total}"] == "Итого"
    for m in MODELS:
        c = L[m]
        assert formulas[f"{c}{total}"] == f"SUM({c}{first}:{c}{last})"   # итог модели формулой SUM
        assert cells[f"{c}{total}"] == SUMS[m]                             # и совпадает с «Итого за смену» на экране
        vals = [cells.get(f"{c}{r}") for r in range(first, last + 1)]
        assert vals[1] is None                                             # вторая строка интервала 1 пустая
    # подсказка в шапке столбца
    xml = " ".join(z.read(nm).decode("utf-8") for nm in z.namelist() if "comments" in nm)
    assert "Количество кузовов модели A01 за интервал" in xml
    # соседние итоги не сдвинулись: «Факт» суммирует именно факт
    assert formulas[f"{L['Факт']}{total}"] == f"SUM({L['Факт']}{first}:{L['Факт']}{last})" and cells[f"{L['Факт']}{total}"] == 9
    assert formulas[f"{L['±']}{total}"] == f"{L['Факт']}{total}-{L['План']}{total}"


async def test_rest_row_under_model_columns(app, anon, pool, live):
    scr = await _screen(pool)
    await _hourly(pool, scr)
    cells, formulas, _ = _cells(await _book(anon, scr))
    pv_headers = history.day_headers(False, True)
    row, L = _table(cells, pv_headers)
    total = next(int(re.match(r"[A-Z]+(\d+)", r).group(1)) for r, v in cells.items() if v == "Итого")
    rest = total + 1
    assert cells[f"A{rest}"] == REST_TITLE
    assert [cells.get(f"{L[m]}{rest}") for m in MODELS] == [5.0, 6.0, 7.0, 8.0]     # число под своей моделью (A01, P01, CS55, A13T)
    in_row = {r: v for r, v in cells.items() if re.fullmatch(r"[A-Z]+" + str(rest), r)}
    assert set(in_row) == {f"A{rest}"} | {f"{L[m]}{rest}" for m in MODELS}          # остальные ячейки пустые
    assert not [r for r in formulas if r.endswith(str(rest)) and re.fullmatch(r"[A-Z]+" + str(rest), r)]   # без формул
    for f in formulas.values():                                                    # в SUM строка остатка не входит
        for end in re.findall(r"SUM\([A-Z]+\d+:[A-Z]+(\d+)\)", f):
            assert int(end) < total


async def test_rest_row_dash_without_link(app, anon, pool, monkeypatch):
    monkeypatch.setattr(rb, "plant_today", lambda: DAY)

    async def boom(plc_id, nodes):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(rb, "read_poller_values", boom)
    scr = await _screen(pool)
    await _hourly(pool, scr)
    cells, formulas, _ = _cells(await _book(anon, scr))
    row, L = _table(cells, history.day_headers(False, True))
    total = next(int(re.match(r"[A-Z]+(\d+)", r).group(1)) for r, v in cells.items() if v == "Итого")
    assert [cells.get(f"{L[m]}{total + 1}") for m in MODELS] == ["—"] * 4
    # нет значений моделей: в интервалах «—», итог «—», формул SUM по моделям нет
    assert [cells.get(f"{L[m]}{row + 1}") for m in MODELS] == ["—"] * 4
    assert [cells.get(f"{L[m]}{total}") for m in MODELS] == ["—"] * 4
    assert not [f for r, f in formulas.items() if re.match(rf"[{L['A01']}-{L['A13T']}]{total}$", r)]


async def test_past_day_models_dashes_and_no_rest(app, anon, pool, live):
    scr = await _screen(pool)
    past = date(2026, 10, 4)
    await _hourly(pool, scr, past)
    cells, _, _ = _cells(await _book(anon, scr, past))
    row, L = _table(cells, history.day_headers(False, True))
    assert [cells.get(f"{L[m]}{row + 1}") for m in MODELS] == ["—"] * 4
    assert REST_TITLE not in cells.values()


async def test_with_starve_order_matches_screen(app, anon, pool, live):
    scr = await _screen(pool, starve=True)
    await _hourly(pool, scr)
    h = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()["headers"]
    assert h[h.index("±") + 1:h.index("Отставание мин")] == MODELS + ["Takt Time", "Простой RB262"]
    cells, formulas, _ = _cells(await _book(anon, scr))
    row, L = _table(cells, h)
    n = 4
    total = row + n + 1
    assert formulas[f"{L['Отставание мин']}{total}"].startswith("SUM(")
    assert formulas[f"{L['Простой RB262']}{total}"].startswith("SUM(")


async def test_line_without_models_keeps_old_table(app, anon, pool, live):
    scr = await _screen(pool, name="GWM", template="body_counter", models=False)
    await _hourly(pool, scr)
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert pv["headers"] == history.DAY_HEADERS
    assert not any(m in pv["headers"] for m in MODELS)
    cells, _, _ = _cells(await _book(anon, scr))
    assert REST_TITLE not in cells.values()
    _table(cells, history.DAY_HEADERS)


async def test_autosave_report_has_models_and_rest_under_them(app, pool, live):
    from combined import build_combined_report
    scr = await _screen(pool)
    await _hourly(pool, scr)
    cells, formulas, _ = _cells((await build_combined_report(pool, scr, DAY))[0])
    row, L = _table(cells, history.day_headers(False, True))
    total = row + 5
    assert cells[f"A{total}"] == "Итого"
    assert [cells.get(f"{L[m]}{total}") for m in MODELS] == [SUMS[m] for m in MODELS]
    assert formulas[f"{L['A01']}{total}"] == f"SUM({L['A01']}{row + 1}:{L['A01']}{row + 4})"
    assert [cells.get(f"{L[m]}{total + 1}") for m in MODELS] == [5.0, 6.0, 7.0, 8.0]
    assert cells[f"A{total + 1}"] == REST_TITLE


# ---------- экран соответствует файлу ----------

def test_screen_columns_equal_excel_headers():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    fl = html[html.index("function buildFlLayout"):html.index("function openPlanDialog")]
    h = history.day_headers(True, True)
    on_screen = [t for t in ("№", "Время", "Мин.", "План", "Факт", "±", "Takt Time", "Простой RB262", "Отставание, мин", "Причина") if f">{t}</th>" in fl]
    assert fl.index("±</th>") < fl.index("FL_MODELS.map(m => `<th") < fl.index("Takt Time</th>")   # модели между «±» и «Takt Time»
    assert [x for x in h if x in on_screen] == [x for x in on_screen if x in h]
    assert h[h.index("±") + 1:h.index("Takt Time")] == MODELS
    models = json.loads(re.search(r"const FL_MODELS = (\[[^\]]+\])", html).group(1).replace("'", '"'))
    assert models == flmodels.MODELS
    assert "Количество кузовов модели ${m} за интервал" in fl and flmodels.MODEL_HINT == "Количество кузовов модели {m} за интервал"
    assert fl.index("Итого за смену") < fl.index(">Остаток кузовов на ФЛ</td>")


def test_dockerfile_copies_flmodels():
    assert "COPY fastapi-api/*.py ./" in (ROOT / "fastapi-api" / "Dockerfile").read_text(encoding="utf-8")   # flmodels.py попадает в образ общим COPY
