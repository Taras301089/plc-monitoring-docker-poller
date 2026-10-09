"""Excel-выгрузки повторяют экран: названия графиков и подписи плиток в файле равны тем, что в static/*.js.

Список названий на экране берётся из исходников страниц, поэтому новый график на экране без графика в файле роняет тест."""
import io
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
DAY = date(2026, 10, 5)
D0 = date(2026, 10, 1)


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def chart_titles(content: bytes) -> list[str]:
    """Названия диаграмм книги в порядке xl/charts/chartN.xml (первый c:title каждой диаграммы)."""
    z = zipfile.ZipFile(io.BytesIO(content))
    names = sorted((n for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    out = []
    for n in names:
        xml = z.read(n).decode("utf-8")
        m = re.search(r"<c:title>(.*?)</c:title>", xml, re.S)
        out.append("".join(re.findall(r"<a:t>(.*?)</a:t>", m.group(1))) if m else "")
    return out


def tile_labels(content: bytes) -> list[str]:
    """Подписи плиток: текстовые ячейки третьей строки первого листа слева направо."""
    z = zipfile.ZipFile(io.BytesIO(content))
    shared = re.findall(r"<si><t[^>]*>(.*?)</t></si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)
    cells = re.findall(r'<c r="([A-Z]+)3"([^>/]*)>(.*?)</c>', z.read("xl/worksheets/sheet1.xml").decode("utf-8"), re.S)
    out = []
    for _, attrs, body in sorted(cells, key=lambda c: (len(c[0]), c[0])):
        v = re.search(r"<v>(.*?)</v>", body)
        if v and 't="s"' in attrs:
            out.append(shared[int(v.group(1))])
    return out


def _calls(text: str, fn: str) -> list[str]:
    """Первые аргументы-строки вызовов fn('...' в тексте."""
    return re.findall(rf"\b{fn}\('([^']+)'", text)


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a:text.index(end, a)]


# ---------- экран: что показывает страница ----------

def screen_charts_cards() -> list[str]:
    return re.findall(r"kind: '\w+', title: '([^']+)'", _src("charts.js"))


def screen_charts_tiles() -> list[str]:
    return _calls(_section(_src("charts.js"), "function summaryTiles", "function build"), "t")


def screen_trends_cards() -> list[str]:
    return re.findall(r'<h3[^>]*>([^<]+)</h3>\s*<button[^>]*id="tr-xl"', _src("trends.js"))


def screen_trends_tiles() -> list[str]:
    return _calls(_section(_src("trends.js"), "function tiles", "function ticks"), "t")


def screen_reports_chart_titles() -> list[str]:
    return re.findall(r"q\('chart-title'\)\.textContent = '([^']+)'", _src("reports.js"))


def screen_reports_tiles(fn: str, nxt: str) -> list[str]:
    end = f"async function {nxt}" if nxt else "async function loadReport"
    return _calls(_section(_src("reports.js"), f"async function {fn}", end), "tile")


def test_screen_sources_parsed():
    """Разбор исходников экрана не пустой (иначе остальные проверки были бы пустыми)."""
    assert len(screen_charts_cards()) == 5 and len(screen_charts_tiles()) == 6
    assert screen_trends_cards() == ["Связь с ПЛК"] and len(screen_trends_tiles()) == 3
    assert len(screen_reports_chart_titles()) == 2
    assert len(screen_reports_tiles("drawDay", "drawPeriod")) == 4 and len(screen_reports_tiles("drawPeriod", "")) == 6


# ---------- данные ----------

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


async def _get(anon, url, params):
    r = await anon.get(url, params=params)
    assert r.status_code == 200, r.text
    return r.content


# ---------- «Графики» ----------

async def test_charts_period_one_day_has_all_screen_charts(app, anon, pool):
    scr = await _seed(pool)
    book = await _get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": DAY.isoformat(), "to": DAY.isoformat(), "src": "charts"})
    assert chart_titles(book) == screen_charts_cards()
    assert tile_labels(book) == screen_charts_tiles()


async def test_charts_period_several_days_hides_cumulative(app, anon, pool):
    scr = await _seed(pool)
    book = await _get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": D0.isoformat(), "to": DAY.isoformat(), "src": "charts"})
    # накопленное за день на экране при периоде из нескольких дней скрыто
    assert chart_titles(book) == screen_charts_cards()[:4]
    assert tile_labels(book) == screen_charts_tiles()


async def test_charts_each_chart_export_has_its_screen_title(app, anon, pool):
    scr = await _seed(pool)
    kinds = re.findall(r"kind: '(\w+)', title:", _src("charts.js"))
    for kind, title in zip(kinds, screen_charts_cards()):
        d1 = DAY if kind == "cumulative" else D0
        book = await _get(anon, f"/api/kpi/screens/{scr}/chart.xlsx", {"kind": kind, "from": d1.isoformat(), "to": DAY.isoformat()})
        assert chart_titles(book) == [title], kind


async def test_cumulative_legend_like_screen(app, anon, pool):
    scr = await _seed(pool)
    book = await _get(anon, f"/api/kpi/screens/{scr}/chart.xlsx", {"kind": "cumulative", "from": DAY.isoformat(), "to": DAY.isoformat()})
    xml = zipfile.ZipFile(io.BytesIO(book)).read("xl/charts/chart1.xml").decode("utf-8")
    legend = re.search(r'<div class="ch-legend"><i class="ch-l-plan"></i>(.*?)</div>', _src("charts.js")).group(1)
    names = re.findall(r"(?:^|</i>)([^<]+)", legend)
    assert len(names) == 4
    for name in names:
        assert f"<c:v>{name}</c:v>" in xml, name


# ---------- «Отчёты» ----------

async def test_reports_day_chart_and_tiles(app, anon, pool):
    scr = await _seed(pool)
    book = await _get(anon, f"/api/kpi/screens/{scr}/day.xlsx", {"date": DAY.isoformat(), "src": "reports"})
    assert chart_titles(book) == [screen_reports_chart_titles()[0]]
    assert tile_labels(book) == screen_reports_tiles("drawDay", "drawPeriod")
    # линия «% выполнения» есть и в файле, как в легенде экрана
    xml = zipfile.ZipFile(io.BytesIO(book)).read("xl/charts/chart1.xml").decode("utf-8")
    assert "<c:lineChart>" in xml and "<c:v>% выполнения</c:v>" in xml


async def test_reports_period_chart_and_tiles(app, anon, pool):
    scr = await _seed(pool)
    book = await _get(anon, f"/api/kpi/screens/{scr}/period.xlsx", {"from": D0.isoformat(), "to": DAY.isoformat(), "src": "reports"})
    assert screen_reports_chart_titles()[1] in chart_titles(book)
    assert tile_labels(book) == screen_reports_tiles("drawPeriod", "")


# ---------- «Andon», кнопка Excel: те же выгрузки дня и периода ----------

def test_andon_excel_is_report_export():
    src = _src("period.js")
    assert "day.xlsx" in src and "period.xlsx" in src


# ---------- «Тренды» ----------

async def test_trends_link_chart_and_tiles(app, anon, pool, monkeypatch):
    import trends
    tz = timezone(timedelta(hours=5))
    monkeypatch.setattr(trends, "_now", lambda: datetime(2026, 9, 11, 12, 0, tzinfo=tz))
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ('tr_x', 'opc.tcp://10.0.0.1:4840', TRUE) RETURNING id")
    for state, h in (("ok", 0), ("no_link", 10), ("ok", 11)):
        await pool.execute("INSERT INTO plc_link_events (plc_id, state, reason, started_at) VALUES ($1, $2, '', $3)",
                           plc, state, datetime(2026, 9, 10, h, 0, tzinfo=tz))
    await pool.execute("INSERT INTO plc_connection_status (plc_id, is_connected, updated_at) VALUES ($1, TRUE, $2)", plc, datetime(2026, 9, 11, 11, 59, tzinfo=tz))
    book = await _get(anon, "/api/trends/link.xlsx", {"from": "2026-09-10", "to": "2026-09-10"})
    assert chart_titles(book) == screen_trends_cards()
    assert tile_labels(book) == screen_trends_tiles()
