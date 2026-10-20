"""Словарь названий интерфейса (T-14): API и права, серверная label(), стандартные названия в коде, экран = Excel при переименовании."""
import io
import re
import zipfile
from pathlib import Path

import pytest

import test_excel_matches_screen as ex

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
DAY, D0 = ex.DAY, ex.D0


def _labels():
    import labels
    return labels


@pytest.fixture(autouse=True)
def _clean_cache():
    _labels().reset_cache()
    ex.CUSTOM.clear()
    yield
    _labels().reset_cache()
    ex.CUSTOM.clear()


# ---------- API и права ----------

async def test_get_is_public_and_empty_by_default(app, anon):
    r = await anon.get("/api/ui-labels")
    assert r.status_code == 200 and r.json() == {"labels": {}}


@pytest.mark.parametrize("login", ["t_viewer", "t_master", "t_oto", "t_area", "t_chief"])
async def test_only_admin_can_change(app, users, login):
    c = users[login]
    assert (await c.put("/api/ui-labels/down_min", json={"text": "Моё"})).status_code == 403
    assert (await c.delete("/api/ui-labels/down_min")).status_code == 403
    assert (await c.get("/api/ui-labels/catalog")).status_code == 403


async def test_anonymous_cannot_change(app, anon):
    assert (await anon.put("/api/ui-labels/down_min", json={"text": "Моё"})).status_code in (401, 403)
    assert (await anon.delete("/api/ui-labels/down_min")).status_code in (401, 403)
    assert (await anon.get("/api/ui-labels/catalog")).status_code in (401, 403)


async def test_admin_sets_reads_and_resets(app, users, anon, pool):
    a = users["t_admin"]
    r = await a.put("/api/ui-labels/down_min", json={"text": "Потери, мин", "title": "Потери времени за интервал в минутах, считаются по плану и факту"})
    assert r.status_code == 200
    got = (await anon.get("/api/ui-labels")).json()["labels"]
    assert got == {"down_min": {"text": "Потери, мин", "title": "Потери времени за интервал в минутах, считаются по плану и факту"}}
    row = await pool.fetchrow("SELECT text, updated_by_name FROM ui_labels WHERE key = 'down_min'")
    assert row["text"] == "Потери, мин" and row["updated_by_name"]
    assert (await a.delete("/api/ui-labels/down_min")).status_code == 200
    assert (await anon.get("/api/ui-labels")).json() == {"labels": {}}
    log = await pool.fetch("SELECT was_text, now_text, by_name FROM ui_labels_log WHERE key = 'down_min' ORDER BY id")
    assert [(x["was_text"], x["now_text"]) for x in log] == [("", "Потери, мин"), ("Потери, мин", "")] and all(x["by_name"] for x in log)


async def test_text_limit_40_chars(app, users):
    a = users["t_admin"]
    assert (await a.put("/api/ui-labels/down_min", json={"text": "я" * 40})).status_code == 200
    assert (await a.put("/api/ui-labels/down_min", json={"text": "я" * 41})).status_code == 422


async def test_unknown_key_rejected(app, users):
    assert (await users["t_admin"].put("/api/ui-labels/no_such_key", json={"text": "x"})).status_code == 404


async def test_catalog_for_admin_lists_every_label(app, users):
    items = (await users["t_admin"].get("/api/ui-labels/catalog")).json()["items"]
    assert len(items) >= 24 and items[0]["key"] == "down_min" and items[0]["text"] == "Отставание, мин"
    assert items[0]["title"].startswith("Отставание от плана, мин: (план − факт) ÷ план × длительность интервала; включает остановки и медленную работу")


# ---------- серверная label() ----------

def test_label_defaults_with_empty_cache():
    L = _labels()
    assert L.label("down_min") == "Отставание, мин"
    assert L.label("down_min", "Своё стандартное") == "Своё стандартное"
    assert L.label("нет_такого", "D") == "D" and L.label("нет_такого") == ""


async def test_label_survives_unavailable_db():
    class Broken:
        async def fetch(self, *a, **k):
            raise ConnectionError("база недоступна")

    L = _labels()
    await L.refresh(Broken(), force=True)
    assert L.label("down_min") == "Отставание, мин"


async def test_label_follows_dictionary_and_resets(app, users, pool):
    L = _labels()
    await users["t_admin"].put("/api/ui-labels/down_min", json={"text": "Потери, мин"})
    assert L.label("down_min") == "Потери, мин"
    await users["t_admin"].delete("/api/ui-labels/down_min")
    assert L.label("down_min") == "Отставание, мин"


async def test_label_cache_lasts_5_seconds(app, pool):
    L = _labels()
    await pool.execute("INSERT INTO ui_labels (key, text) VALUES ('tile_plan', 'Норма')")
    await L.refresh(pool, force=True)
    await pool.execute("UPDATE ui_labels SET text = 'Другое' WHERE key = 'tile_plan'")
    await L.refresh(pool)
    assert L.label("tile_plan") == "Норма"      # в пределах 5 с берётся кэш
    L._cache["at"] -= 6
    await L.refresh(pool)
    assert L.label("tile_plan") == "Другое"


# ---------- стандартные названия в коде совпадают со словарём ----------

def _sources():
    for p in list(STATIC.glob("*.js")) + [STATIC / "index.html"]:
        yield p, p.read_text(encoding="utf-8")


def test_defaults_in_code_equal_catalog():
    L = _labels()
    seen = set()
    for p, text in _sources():
        for key, default in re.findall(r"\b(?:L|uiLabel)\('(\w+)', '([^']+)'\)", text):
            assert key in L.BY_KEY, f"{p.name}: ключа {key} нет в словаре"
            assert default == L.BY_KEY[key]["text"], f"{p.name}: {key}: {default!r} != {L.BY_KEY[key]['text']!r}"
            seen.add(key)
        for key, default in re.findall(r'data-ui-label="(\w+)"[^>]*>(?:[^<\w]*)([^<]*)</', text):
            assert key in L.BY_KEY, f"{p.name}: ключа {key} нет в словаре"
            assert default.strip() == L.BY_KEY[key]["text"], f"{p.name}: {key}: {default!r}"
            seen.add(key)
    py = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "fastapi-api").glob("*.py") if p.name != "labels.py")
    seen |= set(re.findall(r'\bui_label\("(\w+)"', py))
    assert seen == set(L.BY_KEY), f"подписи словаря без места использования: {set(L.BY_KEY) - seen}"


def test_catalog_titles_have_10_to_20_words_where_new():
    L = _labels()
    for c in L.CATALOG:
        if c["title"]:
            assert (c["key"].startswith("tab_") or len(c["title"].split()) >= 10) and len(c["title"].split()) <= 25, c["key"]   # подсказки вкладок прежние
        assert len(c["text"]) <= L.MAX_TEXT


def test_chart_titles_come_from_dictionary():
    js = (STATIC / "charts.js").read_text(encoding="utf-8")
    assert len(re.findall(r"kind: '\w+', get title\(\) \{ return L\('chart_\w+', '[^']+'\)", js)) == 5
    assert "title: '" not in js.split("const CARDS")[1].split("];")[0]
    assert "uiLabel" in js and "labels.js" in (STATIC / "index.html").read_text(encoding="utf-8")


def test_labels_js_contract():
    js = (STATIC / "labels.js").read_text(encoding="utf-8")
    assert "window.uiLabel" in js and "data-ui-label" in js and "data-ui-title" in js
    assert "AbortSignal.timeout(6000)" in js and "10000" in js
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<script src="/static/labels\.js"></script>', html)
    assert html.index("labels.js") < html.index("/static/charts.js")


def test_forbidden_words_stay_out_of_dictionary():
    L = _labels()
    for c in L.CATALOG:
        assert c["text"] not in ("Простой RB262", "Простой BDC", "Простой весь день (поломка линии)")


# ---------- экран = Excel и при изменённых названиях ----------

RENAMES = {
    "down_min": "Потери, мин", "chart_down_days": "Потери по дням", "chart_down_stations": "Потери по станциям",
    "chart_plan_fact": "Норма и выпуск по дням", "tile_plan": "Норма", "tile_fact": "Выпуск", "tile_days": "Рабочих дней",
    "chart_plan_fact_hour": "Норма и выпуск по часам", "chart_pareto": "Парето потерь", "chart_cumulative": "Нарастающий итог",
    "tile_exec": "Исполнение", "tile_undescribed": "Без причины, мин",
}


async def test_excel_matches_screen_with_renamed_labels(app, users, anon, pool):
    scr = await ex._seed(pool)
    for k, v in RENAMES.items():
        assert (await users["t_admin"].put(f"/api/ui-labels/{k}", json={"text": v})).status_code == 200
    ex.CUSTOM.update(RENAMES)
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": DAY.isoformat(), "to": DAY.isoformat(), "src": "charts"})
    assert ex.chart_titles(book) == ex.screen_charts_cards()
    assert ex.chart_titles(book)[1] == "Потери по дням"
    assert ex.tile_labels(book) == ex.screen_charts_tiles() and "Потери, мин" in ex.tile_labels(book)
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": D0.isoformat(), "to": DAY.isoformat(), "src": "reports"})
    assert ex.screen_reports_chart_titles()[1] in ex.chart_titles(book)
    assert ex.tile_labels(book) == ex.screen_reports_tiles("drawPeriod", "")
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/day.xlsx", {"date": DAY.isoformat(), "src": "reports"})
    assert ex.chart_titles(book) == [ex.screen_reports_chart_titles()[0]] == ["Норма и выпуск по часам"]
    assert ex.tile_labels(book) == ex.screen_reports_tiles("drawDay", "drawPeriod") == ["Норма", "Выпуск", "Исполнение", "Потери, мин"]
    kinds = re.findall(r"kind: '(\w+)', get title", (STATIC / "charts.js").read_text(encoding="utf-8"))
    for kind, title in zip(kinds, ex.screen_charts_cards()):
        d1 = DAY if kind == "cumulative" else D0
        book = await ex._get(anon, f"/api/kpi/screens/{scr}/chart.xlsx", {"kind": kind, "from": d1.isoformat(), "to": DAY.isoformat()})
        assert ex.chart_titles(book) == [title], kind


async def test_excel_series_and_headers_use_dictionary(app, users, anon, pool):
    scr = await ex._seed(pool)
    await users["t_admin"].put("/api/ui-labels/down_min", json={"text": "Потери, мин"})
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": D0.isoformat(), "to": DAY.isoformat(), "src": "charts"})
    z = zipfile.ZipFile(io.BytesIO(book))
    xml = "".join(z.read(n).decode("utf-8") for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n))
    shared = z.read("xl/sharedStrings.xml").decode("utf-8")
    assert "<c:v>Потери, мин</c:v>" in xml and "Отставание, мин" not in xml and "Отставание, мин" not in shared


async def test_default_excel_uses_new_standard_name(app, anon, pool):
    scr = await ex._seed(pool)
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": D0.isoformat(), "to": DAY.isoformat(), "src": "reports"})
    shared = zipfile.ZipFile(io.BytesIO(book)).read("xl/sharedStrings.xml").decode("utf-8")
    assert "Отставание, мин" in shared and "Простой, мин" not in shared


async def test_day_table_down_header_default_and_renamed(app, users, anon, pool):
    """Заголовок колонки «Отставание мин» в таблице дня (экран «Отчёты» и Excel) идёт отдельным ключом словаря col_down_report;
    служебные поиски колонки не зависят от текста (колонка стоит перед «Отв.»)."""
    scr = await ex._seed(pool)
    p = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert "Отставание мин" in p["headers"] and "Простой мин" not in p["headers"]
    assert p["headers"].index("Отставание мин") == p["headers"].index("Отв.") - 1
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/day.xlsx", {"date": DAY.isoformat(), "src": "reports"})
    shared = zipfile.ZipFile(io.BytesIO(book)).read("xl/sharedStrings.xml").decode("utf-8")
    assert "Отставание мин" in shared and "Простой мин" not in shared
    assert (await users["t_admin"].put("/api/ui-labels/col_down_report", json={"text": "Потери мин"})).status_code == 200
    _labels().reset_cache()
    p = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert p["headers"].index("Потери мин") == p["headers"].index("Отв.") - 1 and "Отставание мин" not in p["headers"]
    book = await ex._get(anon, f"/api/kpi/screens/{scr}/day.xlsx", {"date": DAY.isoformat(), "src": "reports"})
    shared = zipfile.ZipFile(io.BytesIO(book)).read("xl/sharedStrings.xml").decode("utf-8")
    assert "Потери мин" in shared and "Отставание мин" not in shared


def test_reports_js_finds_down_column_by_position_not_text():
    js = (STATIC / "reports.js").read_text(encoding="utf-8")
    assert "indexOf('Отв.') - 1" in js and "indexOf('Простой мин')" not in js
