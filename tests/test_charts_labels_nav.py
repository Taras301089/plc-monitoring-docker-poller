"""Вкладка «Графики»: подписи плана, легенда Парето, кнопки «⬇ Excel» одного вида, режим «День» (◀ ▶, одна дата).

Логика charts.js проверяется через node (чистые функции), вид и Excel: по исходникам и по файлам выгрузки."""
import io
import json
import re
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
JS = (STATIC / "charts.js").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")
REF = (STATIC / "reference.html").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
DAY = date(2026, 10, 5)
D0 = date(2026, 10, 1)


def run(expr):
    code = f"const c=require({json.dumps(str(STATIC / 'charts.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


def days(n, plan=20, fact=18):
    return [{"date": f"2026-10-{i + 1:02d}", "plan": plan, "fact": fact, "pct": fact / plan, "downtime": 5, "described": 3} for i in range(n)]


# ---------- 1. подписи плана ----------

@needs_node
def test_plan_values_labelled_like_fact():
    svg = run(f"c.chartPlanFact({json.dumps(days(5))}, 900)")
    plan = re.findall(r'class="ch-val ch-val-plan"[^>]*>(\d+)<', svg)
    fact = re.findall(r'class="ch-val" [^>]*>(\d+)<', svg)
    assert plan == ["20"] * 5 and fact == ["18"] * 5


@needs_node
def test_plan_label_sits_over_plan_bar_not_over_fact():
    svg = run(f"c.chartPlanFact({json.dumps(days(1))}, 600)")
    plan_bar = re.search(r'<rect class="ch-plan" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"', svg)
    x, y, w = (float(v) for v in plan_bar.groups())
    lab = re.search(r'class="ch-val ch-val-plan" x="([\d.]+)" y="([\d.]+)"', svg)
    assert abs(float(lab.group(1)) - (x + w / 2)) < 0.01 and float(lab.group(2)) < y


@needs_node
def test_plan_labels_hidden_when_they_would_overlap():
    # много дней: подписей нет совсем (как у факта); три цифры при узкой паре столбцов: подпись плана не рисуется
    assert "ch-val" not in run(f"c.chartPlanFact({json.dumps(days(31))}, 560)")
    wide = run(f"c.chartPlanFact({json.dumps(days(9, 120, 110))}, 560)")
    assert "ch-val-plan" not in wide
    assert "ch-val-plan" in run(f"c.chartPlanFact({json.dumps(days(3, 120, 110))}, 900)")


def test_plan_label_color_from_variable():
    assert re.search(r"\.ch-svg \.ch-val-plan \{ fill: var\(--fg-secondary\); \}", CSS)


def test_reports_tab_uses_same_plan_fact_chart():
    """«Отчёты» рисуют тот же график функцией charts.js, поэтому подписи плана там появляются автоматически."""
    assert "window.andonChart.planFact" in (STATIC / "reports.js").read_text(encoding="utf-8")


# ---------- 2. легенда Парето ----------

def test_pareto_legend_and_title_on_screen():
    # легенда собирается функцией: название ряда берётся из словаря «Названия» (стандартное «Отставание, мин»)
    m = re.search(r"const PARETO_LEGEND = \(\) => '<div class=\"ch-legend\" title=\"([^\"]+)\">(.*?)</div>';", JS)
    assert m
    assert m.group(1) == ("Накопленная доля причин простоя в процентах от суммы всех причин; "
                          "показывает, сколько простоя дают первые причины")
    body = m.group(2).replace("' + L('down_min', 'Отставание, мин') + '", "Отставание, мин")
    names = re.findall(r"(?:</i>|^)([^<]+)", body)
    assert names == ["Отставание, мин", "Накопленный %"]
    assert "ch-l-cum" in body and "PARETO_LEGEND" in JS.split("kind === 'reasons'")[1][:40]


def test_pareto_legend_line_is_gray_like_the_chart():
    assert re.search(r"\.ch-l-cum \{ background: var\(--fg-secondary\)", CSS)
    assert re.search(r"\.ch-svg \.ch-line-b \{ stroke: var\(--fg-secondary\)", CSS)


async def _seed(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    for day in (DAY, D0):
        for i, (p, f) in enumerate(zip([4, 4, 5], [3, 5, 5]), start=1):
            await pool.execute(
                "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), p, f)
        await pool.execute(
            "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 4, 3, 60, 20)", scr, day)
    return scr


async def _chart_xml(anon, scr, kind, d1, d2):
    r = await anon.get(f"/api/kpi/screens/{scr}/chart.xlsx", params={"kind": kind, "from": d1.isoformat(), "to": d2.isoformat()})
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content)).read("xl/charts/chart1.xml").decode("utf-8")


async def test_excel_pareto_legend_names_like_screen(app, anon, pool):
    scr = await _seed(pool)
    xml = await _chart_xml(anon, scr, "reasons", D0, DAY)
    legend = re.search(r"const PARETO_LEGEND = \(\) => '(.*?)';", JS).group(1).replace("' + L('down_min', 'Отставание, мин') + '", "Отставание, мин")
    names = re.findall(r"(?:</i>|^)([^<]+)", re.sub(r'^<div[^>]*>|</div>$', "", legend))
    for name in names:
        assert f"<c:v>{name}</c:v>" in xml, name
    assert "<c:legend>" in xml and "<c:lineChart>" in xml


async def test_excel_plan_fact_has_data_labels_for_plan_and_fact(app, anon, pool):
    scr = await _seed(pool)
    xml = await _chart_xml(anon, scr, "plan_fact", D0, DAY)
    sers = re.findall(r"<c:ser>.*?</c:ser>", xml, re.S)
    by_name = {re.search(r"<c:v>(.*?)</c:v>", s).group(1): s for s in sers}
    assert '<c:showVal val="1"/>' in by_name["План"] and '<c:showVal val="1"/>' in by_name["Факт"]
    assert "<c:dLbls>" in by_name["План"]
    assert "<c:dLbls>" not in by_name["% выполнения"]


# ---------- 3. кнопки «⬇ Excel» одного вида ----------

def test_export_buttons_share_one_style_and_texts():
    page = re.search(r'<button[^>]*id="ch-report"[^>]*>([^<]+)</button>', JS)
    chart = re.search(r'<button[^>]*class="dt-sec ch-xl" data-kind[^>]*>([^<]+)</button>', JS)
    assert page and chart
    assert re.search(r'id="ch-report" class="dt-sec ch-xl"', JS)           # тот же набор классов, что у кнопок графиков
    assert page.group(1) == "⬇ Excel — весь отчёт" and chart.group(1) == "⬇ Excel"
    assert "Отчёт за период" not in JS
    assert ('title="Скачать этот график и его данные в Excel за выбранный период"' in chart.group(0))
    assert ('title="Скачать весь отчёт по линии за период в Excel: сводка с графиками, таблицы по дням, причинам и станциям, список простоев"'
            in page.group(0))
    # одно общее правило размеров: кнопка страницы и кнопки графиков не имеют отдельных стилей
    assert ".ch-bar .ch-xl, .ch-card .ch-xl {" in CSS and "#ch-report {" not in CSS
    # обработчик графиков не цепляет кнопку страницы
    assert ".ch-xl[data-kind]" in JS


def test_reference_mentions_new_button_name():
    assert "Excel — весь отчёт" in REF


# ---------- 4. режим «День» ----------

@needs_node
def test_pick_day_sets_from_and_to_and_clamps_to_today():
    assert run("c.pickDay('2026-10-03','2026-10-12')") == {"mode": "day", "from": "2026-10-03", "to": "2026-10-03"}
    assert run("c.pickDay('2026-10-20','2026-10-12')") == {"mode": "day", "from": "2026-10-12", "to": "2026-10-12"}


@needs_node
def test_day_arrows_move_one_day_and_stop_at_today():
    st = "{mode:'day',from:'2026-10-11',to:'2026-10-11'}"
    assert run(f"c.shiftPeriod({st},-1,'2026-10-12')") == {"mode": "day", "from": "2026-10-10", "to": "2026-10-10"}
    assert run(f"c.shiftPeriod({st},1,'2026-10-12')") == {"mode": "day", "from": "2026-10-12", "to": "2026-10-12"}
    assert run("c.shiftPeriod({mode:'day',from:'2026-10-12',to:'2026-10-12'},1,'2026-10-12')") is None
    # через границу месяца
    assert run("c.shiftPeriod({mode:'day',from:'2026-10-01',to:'2026-10-01'},-1,'2026-10-12')")["from"] == "2026-09-30"


@needs_node
def test_range_arrows_shift_by_period_length():
    wk = "{mode:'range',from:'2026-10-06',to:'2026-10-12'}"
    assert run(f"c.shiftPeriod({wk},-1,'2026-10-12')") == {"mode": "range", "from": "2026-09-29", "to": "2026-10-05"}
    assert run(f"c.shiftPeriod({wk},1,'2026-10-12')") is None
    prev = "{mode:'range',from:'2026-09-01',to:'2026-09-30'}"
    assert run(f"c.shiftPeriod({prev},1,'2026-10-12')") == {"mode": "range", "from": "2026-10-01", "to": "2026-10-12"}


@needs_node
def test_manual_range_does_not_turn_on_day_mode():
    assert run("c.modeOf({mode:'range',from:'2026-10-05',to:'2026-10-05'})") == "range"
    assert run("c.modeOf({mode:'day',from:'2026-10-05',to:'2026-10-05'})") == "day"
    assert run("c.modeOf({from:'2026-10-05',to:'2026-10-05'})") == "day"     # старое сохранение: один день
    assert run("c.modeOf({from:'2026-10-01',to:'2026-10-05'})") == "range"


def test_day_mode_controls_on_screen():
    m = re.search(r'<button[^>]*id="ch-prev"[^>]*title="([^"]{40,})"[^>]*>◀</button>', JS)
    assert m and re.search(r'<button[^>]*id="ch-next"[^>]*title="([^"]{40,})"[^>]*>▶</button>', JS)
    assert re.search(r'<input type="date" id="ch-day" max="\$\{today\(\)\}" title="[^"]{10,}">', JS)
    # дата берётся из времени сервера (plantClock), не из часов браузера
    assert "window.plantClock.isoDate()" in JS and "new Date()" not in JS.replace("new Date(iso", "")
    # в режиме «День» поля «С» и «По» скрыты (одна дата), в диапазоне видны
    assert "#ch-from-l').hidden = day" in JS and "#ch-day-l').hidden = !day" in JS
    assert ".ch-bar label[hidden] { display: none; }" in CSS
