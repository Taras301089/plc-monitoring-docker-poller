"""«Графики переменных»: чистая логика trends.js (через node: пути, ступеньки, разрывы, границы, масштаб, линейка)
и статические проверки экрана и Excel (соответствие названий)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
JS = (STATIC / "trends.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")
PY = (ROOT / "fastapi-api" / "trends.py").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")


def run(expr, pre=""):
    code = f"const c=require({json.dumps(str(STATIC / 'trends.js'))});{pre}console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


def ser(points, **kw):
    """Ряд как с сервера (raw): points [[мс, значение]]."""
    base = {"tag_id": 1, "name": "U", "plc_name": "P", "step": False, "mode": "raw", "gap_sec": 10, "points": points, "prior": None,
            "limit_low": None, "limit_high": None, "deadband": None, "stats": {}}
    base.update(kw)
    return base


PRE = "const N=(s,b)=>c.normalize(s,b||0);"


@needs_node
def test_polyline_breaks_at_gap_over_10s():
    pts = [[0, 230], [1000, 231], [2000, 232], [20000, 229], [21000, 230]]
    s = ser(pts)
    segs = run(f"c.lineSegments(N({json.dumps(s)}),0,30000,30000).map(g=>g.map(p=>[p[0],p[1]]))", PRE)
    assert segs == [[[0, 230], [1000, 231], [2000, 232]], [[20000, 229], [21000, 230]]]
    # ровно 10 с: разрыва нет
    s2 = ser([[0, 1], [10000, 2]])
    assert len(run(f"c.lineSegments(N({json.dumps(s2)}),0,20000,20000)", PRE)) == 1


@needs_node
def test_step_series_holds_value_to_end_and_uses_prior():
    s = ser([[5000, 231], [9000, 232]], step=True, prior=[-100000, 229])
    segs = run(f"c.lineSegments(N({json.dumps(s)}),0,20000,12000).map(g=>g.map(p=>[p[0],p[1]]))", PRE)
    assert segs == [[[0, 229], [5000, 229], [5000, 231], [9000, 231], [9000, 232], [12000, 232]]]
    # без записи до окна ступенька начинается с первой записи; конец не дальше конца окна
    s = ser([[5000, 231]], step=True)
    segs = run(f"c.lineSegments(N({json.dumps(s)}),0,8000,99999).map(g=>g.map(p=>[p[0],p[1]]))", PRE)
    assert segs == [[[5000, 231], [8000, 231]]]
    assert run(f"c.lineSegments(N({json.dumps(ser([], step=True))}),0,8000,8000)", PRE) == []


@needs_node
def test_step_has_no_gaps_even_when_records_far_apart():
    s = ser([[0, 1], [3600000, 2]], step=True)
    assert len(run(f"c.lineSegments(N({json.dumps(s)}),0,4000000,4000000)", PRE)) == 1


@needs_node
def test_agg_gap_threshold_is_2_5_buckets_and_center():
    pts = [[0, 1, 2, 3, 4], [4000, 1, 2, 3, 4], [8000, 1, 2, 3, 4], [24000, 1, 2, 3, 4]]   # корзины по 4 с
    s = ser(pts, mode="agg")
    n = run(f"(()=>{{const o=N({json.dumps(s)},4);return {{t:o.t,gap:o.gapMs,v:o.v,mn:o.mn,mx:o.mx}}}})()", PRE)
    assert n["t"] == [2000, 6000, 10000, 26000] and n["gap"] == 10000
    assert n["v"] == [2, 2, 2, 2] and n["mn"] == [1] * 4 and n["mx"] == [3] * 4
    segs = run(f"c.lineSegments(N({json.dumps(s)},4),0,30000,30000).map(g=>g.length)", PRE)
    assert segs == [3, 1]
    # крупные корзины (60 с): допустимый пропуск растёт до 150 с
    assert run(f"N({json.dumps(s)},60).gapMs", PRE) == 150000


@needs_node
def test_out_of_limits_paths():
    seg = [[0, 230, 230, 230], [1, 231, 231, 231], [2, 260, 260, 260], [3, 261, 261, 261], [4, 232, 232, 232], [5, 233, 233, 233], [6, 200, 200, 200]]
    runs = run(f"c.outPaths({json.dumps(seg)},207,253)")
    assert [[p[0] for p in r] for r in runs] == [[1, 2, 3, 4], [5, 6]]
    assert run(f"c.outPaths({json.dumps(seg[:2])},207,253)") == []
    assert run(f"c.outPaths({json.dumps(seg)},null,null)") == []
    # корзина: среднее внутри, но max выше границы — участок красный
    agg = [[0, 230, 229, 231], [1, 231, 229, 260]]
    assert len(run(f"c.outPaths({json.dumps(agg)},207,253)")) == 1
    assert run("c.outPaths([[0,300,300,300]],207,253)") == [[[0, 300, 300, 300]]]


@needs_node
def test_path_strings():
    seg = [[0, 10, 9, 11], [10, 20, 18, 22]]
    d = run(f"c.pathD({json.dumps(seg)},t=>t,v=>100-v)")
    assert d == "M0,90L10,80"
    b = run(f"c.bandD({json.dumps(seg)},t=>t,v=>100-v)")
    assert b == "M0,89L10,78L10,82L0,91Z"
    assert run("c.bandD([[0,1,1,1]],t=>t,v=>v)") == ""


@needs_node
def test_value_at_nearest_left_and_step_held():
    s = ser([[1000, 230], [2000, 231], [30000, 229]])
    f = lambda t: run(f"c.valueAt(N({json.dumps(s)}),{t},null)", PRE)
    assert f(500) is None
    assert f(1000) == {"t": 1000, "v": 230}
    assert f(1999) == {"t": 1000, "v": 230}
    assert f(2500) == {"t": 2000, "v": 231}
    assert f(15000) is None                     # дыра больше 10 с: значения нет
    assert f(30000) == {"t": 30000, "v": 229}
    st = ser([[1000, 230], [2000, 231]], step=True, prior=[-5000, 228])
    g = lambda t, e="null": run(f"c.valueAt(N({json.dumps(st)}),{t},{e})", PRE)
    assert g(0) == {"t": -5000, "v": 228}
    assert g(1500) == {"t": 1000, "v": 230}
    assert g(3600000) == {"t": 2000, "v": 231}   # действующее значение держится
    assert g(5000, "4000") is None               # дальше «сейчас» значения нет


@needs_node
def test_value_at_binary_search_many_points():
    n = 100000
    expr = f"(()=>{{const p=[];for(let i=0;i<{n};i++)p.push([i*1000,i]);const o=N({{tag_id:1,name:'a',plc_name:'p',step:false,mode:'raw',gap_sec:10,points:p,prior:null,stats:{{}}}});return c.valueAt(o,54321000,null)}})()"
    assert run(expr, PRE) == {"t": 54321000, "v": 54321}


@needs_node
def test_auto_range_not_from_zero_and_flat_and_zero():
    lo, hi = run("c.autoRange([[229,233],[230,236]])")
    assert 227 < lo < 229 and 236 < hi < 239      # 230 В не растягивается от 0
    assert run("c.autoRange([[0,10]],{zero:true})")[0] == 0
    lo, hi = run("c.autoRange([[5,10]],{zero:true})")
    assert lo == 0 and hi > 10
    lo, hi = run("c.autoRange([[230,230]])")
    assert lo < 230 < hi and hi - lo < 5
    assert run("c.autoRange([null,null])") is None and run("c.autoRange([])") is None


@needs_node
def test_view_extent_window_and_step_prior():
    s = ser([[0, 100], [1000, 110], [2000, 120], [9000, 500]])
    assert run(f"c.viewExtent(N({json.dumps(s)}),1000,2500,null)", PRE) == [110, 120]
    st = ser([[1000, 110], [9000, 500]], step=True, prior=[-1000, 90])
    assert run(f"c.viewExtent(N({json.dumps(st)}),0,500,500)", PRE) == [90, 90]
    assert run(f"c.viewExtent(N({json.dumps(st)}),2000,3000,3000)", PRE) == [110, 110]
    assert run(f"c.viewExtent(N({json.dumps(s)}),20000,30000,null)", PRE) is None


@needs_node
def test_nice_ticks_and_time_ticks():
    t = run("c.niceTicks(229.3,232.7,5)")
    assert t["step"] == 1 and t["ticks"][0] == 230 and t["ticks"][-1] == 232
    t = run("c.niceTicks(0,1000,5)")
    assert t["ticks"] == [0, 200, 400, 600, 800, 1000]
    # время завода: UTC+5, ось по времени завода, а не браузера
    ticks = run("c.timeTicks(Date.UTC(2026,9,12,5,0,0),Date.UTC(2026,9,12,9,0,0),5*3600000,8)")
    assert [x["label"] for x in ticks] == ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00"]
    ticks = run("c.timeTicks(Date.UTC(2026,9,8,0,0,0),Date.UTC(2026,9,12,0,0,0),5*3600000,6)")
    assert all(re.fullmatch(r"\d\d\.\d\d( \d\d:\d\d)?", x["label"]) for x in ticks)
    short = run("c.timeTicks(Date.UTC(2026,9,12,5,0,0),Date.UTC(2026,9,12,5,1,0),5*3600000,6)")
    assert re.fullmatch(r"\d\d:\d\d:\d\d", short[0]["label"])


@needs_node
def test_zoom_pan_clamp():
    b = "{a:0,b:1000000}"
    assert run(f"c.zoomView({{a:0,b:1000000}},0.5,500000,{b})") == {"a": 250000, "b": 750000}
    assert run(f"c.zoomView({{a:0,b:1000000}},2,500000,{b})") == {"a": 0, "b": 1000000}      # шире границ нельзя
    z = run(f"c.zoomView({{a:0,b:1000000}},0.0000001,500000,{b})")
    assert z["b"] - z["a"] == 2000                                                       # не мельче MIN_SPAN
    assert run(f"c.panView({{a:100000,b:200000}},-500000,{b})") == {"a": 0, "b": 100000}
    assert run(f"c.panView({{a:100000,b:200000}},950000,{b})") == {"a": 900000, "b": 1000000}
    # под курсором остаётся тот же момент
    v = run(f"c.zoomView({{a:0,b:1000000}},0.5,200000,{b})")
    assert v["a"] == 100000 and v["b"] == 600000


@needs_node
def test_format_and_copy_helpers():
    assert run("c.fmtVal(null)") == "—" and run("c.fmtVal(undefined)") == "—"
    assert run("c.fmtVal(231.45)").replace("\xa0", " ") == "231,5"
    assert run("c.fmtVal(0.5)") == "0,500"
    assert run("c.fmtClock(Date.UTC(2026,9,12,9,5,7),5*3600000)") == "12.10 14:05:07"
    assert run("c.fmtClock(Date.UTC(2026,9,12,9,5,7),5*3600000,false)") == "14:05:07"
    assert run("c.tsv([['Имя','Значение'],['L1',230.5],['a\\tb',null]])") == "Имя\tЗначение\nL1\t230.5\na b\t"
    tags = [{"tag_id": 20, "name": "x"}, {"tag_id": 19, "name": "Energy.Voltage L3-N"}, {"tag_id": 17, "name": "Energy.Voltage L1-N"}, {"tag_id": 18, "name": "Energy.Voltage L2-N"}]
    assert run(f"c.defaultSelection({json.dumps(tags)})") == [17, 18, 19]
    assert run(f"c.defaultSelection({json.dumps(tags[:1])})") == [20]
    assert run("c.colorVar(0)") == "var(--chart-1)" and run("c.colorVar(8)") == "var(--chart-1)"


@needs_node
def test_cursor_nearest_with_aggregated_series():
    pts = [[0, 1, 2, 3, 4], [4000, 5, 6, 7, 4]]
    s = ser(pts, mode="agg")
    assert run(f"c.valueAt(N({json.dumps(s)},4),3000,null)", PRE) == {"t": 2000, "v": 2}
    assert run(f"c.valueAt(N({json.dumps(s)},4),7000,null)", PRE) == {"t": 6000, "v": 6}


# ---------------------------------------------------------------- экран: статические проверки
def test_section_present_and_script_connected():
    assert "Графики переменных" in JS
    assert '/static/trends.js' in INDEX
    assert "api/trends/vars" in JS and "api/trends/series" in JS and "api/trends/series.xlsx" in JS
    assert JS.count("AbortSignal.timeout(6000)") >= 5
    for name in ("Онлайн", "Архив", "День", "Неделя", "Месяц", "Прошлый месяц", "Весь период", "Ось Y от нуля", "Пауза", "Копировать", "Имя", "Значение", "Время",
                 "Минимум", "Среднее", "Максимум", "Точек", "Не удалось загрузить", "Статистика за видимый период"):
        assert name in JS, name


def test_graph_markup_has_titles_and_no_absolute_blocks():
    start = JS.index('id="trg"')
    tpl = JS[start - 200:start + 9000]
    for tag in re.findall(r"<(?:button|input|select|label|th)\b[^>]*>", tpl):
        assert "title=" in tag, tag
    css = [ln for ln in CSS.splitlines() if re.match(r"\s*\.trg", ln)]
    assert css
    for ln in css:
        assert "position: absolute" not in ln and "position:absolute" not in ln, ln
    assert "touch-action" in CSS


def test_chart_palette_in_both_themes_and_matches_excel():
    from trends import CHART_COLORS
    light = re.search(r":root\[data-theme=\"light\"\]\s*\{([^}]*)\}", CSS)
    dark = re.search(r":root\s*\{([^}]*--chart-1[^}]*)\}", CSS)
    assert light and dark
    for i in range(1, 9):
        assert f"--chart-{i}:" in light.group(1) and f"--chart-{i}:" in dark.group(1), i
    got = [re.search(rf"--chart-{i}:\s*(#[0-9a-fA-F]{{6}})", light.group(1)).group(1).lower() for i in range(1, 9)]
    assert got == [c.lower() for c in CHART_COLORS]


def test_no_external_libraries():
    assert not re.search(r"https?://(?!www\.w3\.org)", JS)


def test_excel_names_match_screen():
    """Названия таблицы, колонок и графика на экране и в файле одни и те же."""
    for name in ("Графики переменных", "Статистика за видимый период", "Переменная", "Минимум", "Среднее", "Максимум", "Точек"):
        assert name in JS and name in PY, name
    assert '"График"' in PY and "show_blanks_as" in PY
