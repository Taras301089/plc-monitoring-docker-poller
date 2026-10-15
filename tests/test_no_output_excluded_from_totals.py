"""Дни без выпуска не входят в итоги периода (план, факт, %, число дней) и не рисуются на графике план-факт.

Строки таких дней остаются в таблице «По дням» (с планом и фактом 0, выделены серым): экран «Отчёты», «Графики», Excel и автосохранение
берут итоги из одной функции summarize() (period.py)."""
import re
import zipfile
import io
from datetime import date
from pathlib import Path

import pytest

import dayflags
from test_no_output_day import OFF, TODAY, WORK, _cells, _seed, _shared, _tile_value

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dayflags, "plant_today", lambda: TODAY)


async def _charts(anon, scr, d1, d2):
    return (await anon.get("/api/kpi/charts", params={"screen_id": scr, "from": d1.isoformat(), "to": d2.isoformat()})).json()


async def test_totals_skip_no_output_days_but_keep_rows(app, anon, pool):
    scr = await _seed(pool)
    r = await _charts(anon, scr, WORK, OFF)
    assert [d["no_output"] for d in r["days"]] == [False, True]        # строки обоих дней на месте
    assert [(d["plan"], d["fact"]) for d in r["days"]] == [(13, 12), (13, 0)]   # у дня без выпуска план и факт 0 показываются как есть
    # итоги: только рабочий день; простой по всем дням (у дня без выпуска он не считается, поэтому остаётся 15 минут рабочего дня)
    assert r["totals"] == {"days": 1, "plan": 13, "fact": 12, "downtime": 15.0, "described": 0.0}


async def test_only_no_output_days_give_zero_totals(app, anon, pool):
    scr = await _seed(pool)
    r = await _charts(anon, scr, OFF, OFF)
    assert r["days"] and r["days"][0]["no_output"] is True
    assert r["totals"]["days"] == 0 and r["totals"]["plan"] == 0 and r["totals"]["fact"] == 0


async def test_period_excel_totals_and_chart_skip_no_output_days(app, anon, pool):
    scr = await _seed(pool)
    book = (await anon.get(f"/api/kpi/screens/{scr}/period.xlsx", params={"from": WORK.isoformat(), "to": OFF.isoformat()})).content
    cells = _cells(book)
    # плитки и строка «Итого» совпадают с экраном
    assert _tile_value(cells, "Дней с данными") == 1.0
    assert _tile_value(cells, "План") == 13.0 and _tile_value(cells, "Факт") == 12.0
    total_row = next(re.match(r"[A-Z]+(\d+)", k).group(1) for k, v in cells.items() if v == "Итого" and k.startswith("A"))
    assert cells[f"B{total_row}"] == 13.0 and cells[f"C{total_row}"] == 12.0
    # обе строки в таблице, а график берёт данные со скрытого листа только с днями выпуска
    z = zipfile.ZipFile(io.BytesIO(book))
    wbx = z.read("xl/workbook.xml").decode("utf-8")
    assert re.search(r'<sheet name="График дней" sheetId="\d+" state="hidden"', wbx)
    charts = [z.read(n).decode("utf-8") for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)]
    plan_fact = next(c for c in charts if "% выполнения" in c)
    assert "'График дней'!$B$2" in plan_fact and "$B$3" not in plan_fact   # один день с выпуском, а не два


async def test_chart_export_plan_fact_has_only_days_with_output(app, anon, pool):
    scr = await _seed(pool)
    book = (await anon.get(f"/api/kpi/screens/{scr}/chart.xlsx",
                           params={"kind": "plan_fact", "from": WORK.isoformat(), "to": OFF.isoformat()})).content
    z = zipfile.ZipFile(io.BytesIO(book))
    xml = z.read("xl/charts/chart1.xml").decode("utf-8")
    assert "$B$2" in xml and "$B$3" not in xml


def test_screens_use_server_totals_and_hide_no_output_in_chart():
    rep = (STATIC / "reports.js").read_text(encoding="utf-8")
    cha = (STATIC / "charts.js").read_text(encoding="utf-8")
    assert "d.totals" in rep and "d.days.filter(x => !x.no_output)" in rep and "'rep-noout'" in rep
    assert "d.totals" in cha and "data.days.filter(x => !x.no_output)" in cha
    assert "tr.rep-noout" in (STATIC / "downtime.css").read_text(encoding="utf-8")
