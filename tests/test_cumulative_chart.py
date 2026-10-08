"""График «накопленное производство за день»: накопленные значения, равенство итогам дня, Excel, права."""
import datetime
import io
from urllib.parse import unquote

import re
import zipfile

def _sheet(content: bytes) -> dict[str, str]:
    """Значения ячеек первого листа {A1: текст или число} без внешних библиотек."""
    z = zipfile.ZipFile(io.BytesIO(content))
    shared = re.findall(r"<si><t[^>]*>(.*?)</t></si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)
    out = {}
    for ref, attrs, body in re.findall(r'<c r="([A-Z]+\d+)"([^>]*)>(.*?)</c>', z.read("xl/worksheets/sheet1.xml").decode("utf-8"), re.S):
        v = re.search(r"<v>(.*?)</v>", body)
        if v:
            out[ref] = shared[int(v.group(1))] if 't="s"' in attrs else v.group(1)
    return out


DAY = datetime.date(2026, 10, 5)
PLAN = [4, 4, 5, 0, 6]
FACT = [3, 5, 5, 2, 4]


async def _seed(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    for i, (p, f) in enumerate(zip(PLAN, FACT), start=1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            scr, DAY, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), p, f)
    # интервал без плана и факта не показывается (как в Andon)
    await pool.execute("INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, 6, 780, 840, 0, 0)", scr, DAY)
    return scr


async def test_cumulative_values_and_totals(app, anon, pool):
    scr = await _seed(pool)
    r = await anon.get("/api/kpi/charts/cumulative", params={"screen_id": scr, "date": DAY.isoformat()})
    assert r.status_code == 200
    rows = r.json()["intervals"]
    assert [x["cum_plan"] for x in rows] == [4, 8, 13, 13, 19]
    assert [x["cum_fact"] for x in rows] == [3, 8, 13, 15, 19]
    assert [x["diff"] for x in rows] == [-1, 0, 0, 2, 0]
    assert rows[0]["label"] == "08:00-09:00"
    # итог дня одинаков в Andon (почасовая таблица), предпросмотре «Отчётов» и «Графиках» по дням
    hourly = (await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})).json()["intervals"]
    assert rows[-1]["cum_plan"] == sum(h["plan"] for h in hourly) == sum(PLAN)
    assert rows[-1]["cum_fact"] == sum(h["fact"] for h in hourly) == sum(FACT)
    prev = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert rows[-1]["cum_plan"] == sum(i["plan"] for i in prev["intervals"])
    assert rows[-1]["cum_fact"] == sum(i["fact"] for i in prev["intervals"])
    days = (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": DAY.isoformat(), "to": DAY.isoformat()})).json()["days"]
    assert (days[0]["plan"], days[0]["fact"]) == (rows[-1]["cum_plan"], rows[-1]["cum_fact"])


async def test_cumulative_excel(app, anon, pool):
    scr = await _seed(pool)
    r = await anon.get(f"/api/kpi/screens/{scr}/chart.xlsx",
                       params={"kind": "cumulative", "from": DAY.isoformat(), "to": DAY.isoformat(), "src": "charts"})
    assert r.status_code == 200
    name = unquote(r.headers["content-disposition"].split("''")[1])
    assert name == "Графики_GWM_накопленный_2026-10-05.xlsx"
    ws = _sheet(r.content)
    assert ws["A1"] == "Интервал" and ws["F1"].startswith("Разница")
    assert [int(ws[f"B{i}"]) for i in range(2, 7)] == PLAN and [int(ws[f"C{i}"]) for i in range(2, 7)] == FACT
    assert [int(ws[f"D{i}"]) for i in range(2, 7)] == [4, 8, 13, 13, 19] and [int(ws[f"E{i}"]) for i in range(2, 7)] == [3, 8, 13, 15, 19]
    assert [int(ws[f"F{i}"]) for i in range(2, 7)] == [-1, 0, 0, 2, 0]
    # несколько дней: график показывается за один день
    r2 = await anon.get(f"/api/kpi/screens/{scr}/chart.xlsx", params={"kind": "cumulative", "from": "2026-10-01", "to": DAY.isoformat()})
    assert r2.status_code == 400
