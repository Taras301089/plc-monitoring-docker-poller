"""Автосохранение отчётов: один файл в день на линию, один видимый лист «Отчёт»: сверху отчёт дня, ниже отчёт с начала месяца.

Кнопочные выгрузки (day.xlsx, period.xlsx, chart.xlsx) остаются прежними."""
import io
import re
import zipfile
from datetime import date
from pathlib import Path

DAY = date(2026, 10, 5)
D0 = date(2026, 10, 1)


async def _seed(pool, days=(DAY, D0)):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    for day in days:
        for i, (p, f) in enumerate(zip([4, 4, 5], [3, 5, 5]), start=1):
            await pool.execute(
                "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                scr, day, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), p, f)
        await pool.execute(
            "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 4, 3, 60, 20)", scr, day)
    return scr


# ---------- чтение xlsx без openpyxl ----------

def _zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


def _sheets(content: bytes) -> list[tuple[str, bool]]:
    xml = _zip(content).read("xl/workbook.xml").decode("utf-8")
    return [(m.group(1), 'state="hidden"' in m.group(0)) for m in re.finditer(r'<sheet name="([^"]+)"[^>]*/>', xml)]


def _shared(z: zipfile.ZipFile) -> list[str]:
    xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    return ["".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)) for si in re.findall(r"<si>(.*?)</si>", xml, re.S)]


def _cells(content: bytes) -> dict[tuple[int, str], object]:
    """Ячейки первого листа: {(строка, столбец): значение}; строки из sharedStrings, числа как float, формула даёт кэшированное значение."""
    z = _zip(content)
    shared = _shared(z)
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    out: dict[tuple[int, str], object] = {}
    for m in re.finditer(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', xml, re.S):
        col, row, attrs, body = m.group(1), int(m.group(2)), m.group(3), m.group(4) or ""
        v = re.search(r"<v>(.*?)</v>", body, re.S)
        if not v:
            continue
        out[(row, col)] = shared[int(v.group(1))] if 't="s"' in attrs else (v.group(1) if 't="str"' in attrs else float(v.group(1)))
    return out


def _find_row(cells, text: str, col: str = "A", startswith: bool = True) -> int:
    rows = sorted(r for (r, c), v in cells.items() if c == col and isinstance(v, str) and (v.startswith(text) if startswith else v == text))
    assert rows, f"нет «{text}»"
    return rows[0]


def _all_rows(cells, text: str, col: str = "A") -> list[int]:
    return sorted(r for (r, c), v in cells.items() if c == col and v == text)


def _chart_titles(content: bytes) -> list[str]:
    z = _zip(content)
    names = sorted((n for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    out = []
    for n in names:
        m = re.search(r"<c:title>(.*?)</c:title>", z.read(n).decode("utf-8"), re.S)
        out.append("".join(re.findall(r"<a:t>(.*?)</a:t>", m.group(1))) if m else "")
    return out


async def _save(pool, monkeypatch, tmp_path, scr, day=DAY):
    import reports
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    path, n = await reports.save_day(pool, scr, day)
    assert path is not None
    return path, path.read_bytes()


# ---------- тесты ----------

async def test_file_name_and_no_month_file(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    path, _ = await _save(pool, monkeypatch, tmp_path, scr)
    assert path == tmp_path / "2026-10" / "GWM" / "Отчёт_GWM_2026-10-05.xlsx"
    assert [p.name for p in tmp_path.rglob("*.xlsx")] == ["Отчёт_GWM_2026-10-05.xlsx"]
    assert not list(tmp_path.rglob("*месяц*"))


async def test_one_visible_sheet(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    _, book = await _save(pool, monkeypatch, tmp_path, scr)
    sheets = _sheets(book)
    visible = [n for n, hidden in sheets if not hidden]
    assert visible == ["Отчёт"] and sheets[0][0] == "Отчёт"
    assert all(hidden for n, hidden in sheets if n != "Отчёт")


async def test_sections_order_tiles_and_charts(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    _, book = await _save(pool, monkeypatch, tmp_path, scr)
    cells = _cells(book)
    r_day = _find_row(cells, "День 05.10.2026")
    r_month = _find_row(cells, "С начала месяца (01.10.2026 – 05.10.2026)")
    assert r_day < r_month
    assert r_month - r_day > 10
    # плитки: подписи в третьей строке каждого раздела
    day_tiles = [cells[(r_day + 2, c)] for c in "ACFI" if (r_day + 2, c) in cells]
    assert day_tiles == ["План", "Факт", "Выполнение", "Простой, мин"]
    month_tiles = [cells[(r_month + 2, c)] for c in "ABCDEF"]
    assert month_tiles == ["Дней с данными", "План", "Факт", "Выполнение", "Простой, мин", "Не описано, мин"]
    # таблицы раздела «Месяц» после его плиток, названия как в вебе
    for title in ("По дням", "Причины простоя", "Простой по станциям"):
        assert any(r > r_month for r in _all_rows(cells, title)), title
    assert not any(isinstance(v, str) and "за период" in v for v in cells.values()), "«Takt Time» без «за период»"
    # графики: день, затем период (в том порядке, в каком вставлены)
    titles = _chart_titles(book)
    assert titles[0] == "План и факт по часам"
    assert "План, факт и % выполнения по дням" in titles and "Простой по дням, мин" in titles
    assert "Парето причин простоя (топ-15)".replace("15", "10") in titles or any(t.startswith("Парето причин") for t in titles)
    assert "Простой по станциям (топ-15)" in titles
    assert "Накопленное производство за день" not in titles   # месяц из нескольких дней, как на экране
    assert len(titles) == 5


async def test_cumulative_chart_when_month_is_one_day(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool, days=(D0,))
    _, book = await _save(pool, monkeypatch, tmp_path, scr, day=D0)
    titles = _chart_titles(book)
    assert titles[0] == "План и факт по часам" and "Накопленное производство за день" in titles and len(titles) == 6
    assert sum(1 for _, hidden in _sheets(book) if not hidden) == 1


async def test_sum_formulas(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    _, book = await _save(pool, monkeypatch, tmp_path, scr)
    xml = _zip(book).read("xl/worksheets/sheet1.xml").decode("utf-8")
    sums = re.findall(r"<f>SUM\(([A-Z]+)\d+:[A-Z]+\d+\)</f>", xml)
    # день: F, G, H, K, O; месяц: B, C, F, G, H
    assert sorted(sums) == sorted(["F", "G", "H", "K", "O", "B", "C", "F", "G", "H"])


async def test_numbers_match_api(app, anon, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    _, book = await _save(pool, monkeypatch, tmp_path, scr)
    cells = _cells(book)
    # данные API того же дня и того же периода
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    api_plan = sum(r[6] for r in pv["rows"] if r[6] != "")
    api_fact = sum(r[7] for r in pv["rows"] if r[7] != "")
    ch = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": D0.isoformat(), "to": DAY.isoformat()})).json()
    m_plan, m_fact = sum(d["plan"] for d in ch["days"]), sum(d["fact"] for d in ch["days"])
    m_down = round(sum(d["downtime"] for d in ch["days"]), 1)
    assert (api_plan, api_fact) == (13, 13) and (m_plan, m_fact) == (26, 26)
    # раздел «День»: плитки и строка «Итого»
    r_day = _find_row(cells, "День 05.10.2026")
    assert cells[(r_day + 3, "A")] == api_plan and cells[(r_day + 3, "C")] == api_fact
    r_tot = _all_rows(cells, "Итого")[0]
    assert cells[(r_tot, "G")] == api_plan and cells[(r_tot, "H")] == api_fact
    # заголовки таблицы дня как в вебе
    hdr_row = r_tot - len(pv["rows"]) - 1
    assert [cells[(hdr_row, c)] for c in "ABCDEFGHI"] == pv["headers"][:9]
    # раздел «Месяц»: плитки и «Итого» равны сумме по дням периода
    r_month = _find_row(cells, "С начала месяца")
    assert cells[(r_month + 3, "A")] == len(ch["days"])
    assert cells[(r_month + 3, "B")] == m_plan and cells[(r_month + 3, "C")] == m_fact and cells[(r_month + 3, "E")] == m_down
    r_tot_m = _all_rows(cells, "Итого")[-1]
    assert r_tot_m > r_month
    assert cells[(r_tot_m, "B")] == m_plan and cells[(r_tot_m, "C")] == m_fact and cells[(r_tot_m, "F")] == m_down


async def test_save_now_returns_name_and_path(app, users, pool, monkeypatch, tmp_path):
    import reports
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(reports, "REPORTS_HOST_DIR", str(tmp_path))
    scr = await _seed(pool)
    r = await users["t_admin"].post(f"/api/reports/save-now/{scr}", params={"date": DAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Отчёт_GWM_2026-10-05.xlsx" and body["intervals"] == 3
    assert Path(body["path"]).exists() and Path(body["file"]).name == body["name"]
    assert len(_cells(Path(body["path"]).read_bytes())) > 0


async def test_daily_overwrite_keeps_past_files(app, pool, monkeypatch, tmp_path):
    scr = await _seed(pool)
    p1, _ = await _save(pool, monkeypatch, tmp_path, scr, day=D0)
    p2, _ = await _save(pool, monkeypatch, tmp_path, scr, day=DAY)
    p2b, _ = await _save(pool, monkeypatch, tmp_path, scr, day=DAY)   # повторное сохранение дня перезаписывает файл
    assert p2 == p2b and p1.exists() and p1 != p2
    assert len(list(tmp_path.rglob("*.xlsx"))) == 2


async def test_button_exports_unchanged(app, anon, pool):
    scr = await _seed(pool)
    day = await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})
    assert day.status_code == 200
    assert [n for n, _ in _sheets(day.content)] == ["День", "График"]
    assert _chart_titles(day.content) == ["План и факт по часам"]
    per = await anon.get(f"/api/kpi/screens/{scr}/period.xlsx", params={"from": D0.isoformat(), "to": DAY.isoformat()})
    assert per.status_code == 200
    # видимые листы: отчёт и полный список простоев; служебные данные графиков (например «График дней») только скрытые
    assert [n for n, hidden in _sheets(per.content) if not hidden] == ["Отчёт", "Простои"]
    assert "День 05.10.2026" not in _cells(per.content).values()
