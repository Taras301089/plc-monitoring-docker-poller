"""Вкладка «Аварии»: чистая логика alarms.js (через node) и статические проверки экрана."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "alarms.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
REF = (STATIC / "reference.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def node(expr):
    code = f"const u=require({json.dumps(str(STATIC / 'alarms.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="нет node")
@pytest.mark.parametrize("sec,text", [
    (0, "0 с"), (59, "59 с"), (61, "1 мин 1 с"), (3600, "1 ч"), (94853, "26 ч 20 мин 53 с"),
])
def test_duration(sec, text):
    assert node(f"u.fmtDuration({sec})") == text


@pytest.mark.skipif(not NODE, reason="нет node")
def test_plant_time_offset():
    assert node("u.toPlant('2026-10-08T05:58:17Z', 5)") == "08.10.2026, 10:58:17"
    assert node("u.toPlant('2026-10-08T22:00:00+00:00', 5)") == "09.10.2026, 03:00:00"
    assert node("u.toPlant(null, 5)") == "—"


@pytest.mark.skipif(not NODE, reason="нет node")
def test_url_and_page_label():
    url = node("u.buildUrl('/api/alarms/events', {plc_id:'4', state:'all', station:'', q:'авария МВ170', class:'Ошибки', limit:200, offset:0})")
    assert url.startswith("/api/alarms/events?plc_id=4&state=all&")
    assert "station=" not in url
    assert "%D0%B0%D0%B2%D0%B0%D1%80%D0%B8%D1%8F%20" in url and "class=%D0%9E" in url
    assert "limit=200&offset=0" in url
    assert node("u.pageLabel(2, 200)") == "Страница 2 · показано 200"


@pytest.mark.skipif(not NODE, reason="нет node")
def test_sort_cycle_and_live():
    assert node("u.nextSort(null,'code')") == {"sort": "code", "dir": "asc"}
    assert node("u.nextSort({sort:'code',dir:'asc'},'code')") == {"sort": "code", "dir": "desc"}
    assert node("u.nextSort({sort:'code',dir:'desc'},'code')") is None
    assert node("u.liveSeconds('2026-10-09T00:00:00Z','2026-10-09T00:01:00Z',1000,6000)") == 65


def test_tab_and_container():
    assert re.search(r'<button[^>]*data-tab="alarms"[^>]*title="[^"]{30,}"[^>]*>🚨 Аварии <span class="tab-caret">▾</span></button>', INDEX)
    assert 'id="alarms" class="tab-content"' in INDEX
    assert '/static/alarms.js' in INDEX and '/static/alarms.css' in INDEX
    assert INDEX.index('data-tab="notifications"') < INDEX.index('data-tab="alarms"')


def test_screen_rules():
    assert "AbortSignal.timeout(6000)" in JS
    assert re.search(r"\},\s*5000\);", JS)
    assert "attachColWidths(body.querySelector('table'), 'alarms-journal')" in JS
    assert "root.alarmsUtil = util" in JS and "module.exports" in JS
    assert "xlgrid.toTsv" in JS and "events.xlsx" in JS and "src: 'alarms'" in JS
    assert "Фоновый сбор работает при закрытом браузере" in JS
    assert "Справочник аварий" in JS and "/api/alarms/catalog" in JS
    for t in ("Активные", "История", "Копировать таблицу", "⬇ Excel", "Сбросить"):
        assert t in JS


def test_every_control_has_title():
    for m in re.finditer(r"<(button|select|input)\b[^>]*>", JS):
        assert "title=" in m.group(0), m.group(0)


def test_no_hard_colors():
    css = (STATIC / "alarms.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", css)
    assert not re.search(r"font-size:\s*\d+px", css)


def test_reference_has_alarms():
    assert 'href="index.html#alarms"' in REF and "alarms.js" in REF
    log = REF[REF.index('id="changelog"'):]
    assert re.search(r'<li data-date="\d{4}-\d{2}-\d{2}">', log) and "вкладка Аварии" in log


@pytest.mark.skipif(not NODE, reason="нет node")
@pytest.mark.parametrize("name,brand", [
    ("GWM_UB", "GWM"), ("GWM_MB", "GWM"), ("Chery_UB", "Chery"), ("Changan", "Changan"), ("AE_WBS_2", "AE"),
])
def test_plc_brand(name, brand):
    assert node(f"u.plcBrand({json.dumps(name)})") == brand


@pytest.mark.skipif(not NODE, reason="нет node")
def test_brand_order():
    assert node("u.orderBrands(['AE','Zeta','GWM','Alfa','Changan','Chery'])") == ["GWM", "Chery", "Changan", "AE", "Alfa", "Zeta"]
    assert node("u.orderBrands([])") == ["GWM", "Chery", "Changan", "AE"]


def test_brand_menu_and_plc_buttons():
    assert "BRAND_FIRST = ['GWM', 'Chery', 'Changan', 'AE']" in JS
    assert "id = 'alarms-menu'" in JS and "'Escape'" in JS and "menu.contains(e.target)" in JS
    assert "Для этого ПЛК журнал аварий не настроен" in JS
    assert "localStorage" in JS and "brand" in JS
    assert 'id="al-plcs"' in JS and "al-plc-btn" in JS and 'id="al-plc"' not in JS
    assert re.search(r'data-tab="alarms"[^>]*>🚨 Аварии <span class="tab-caret">▾</span>', INDEX)


def test_header_single_row():
    css = (STATIC / "alarms.css").read_text(encoding="utf-8")
    assert re.search(r"\.al-head\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap", css)
    head = JS[JS.index('<div class="al-head">'):JS.index('<section class="al-card">')]
    for part in ('id="al-title"', 'id="al-plcs"', 'id="al-t-active"', 'id="al-t-history"', 'id="al-period"', 'id="al-chip"'):
        assert part in head
    assert "Журнал событий</div>" not in JS
