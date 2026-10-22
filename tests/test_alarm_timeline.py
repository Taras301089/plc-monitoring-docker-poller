"""Динамика по времени (вкладка «Аварии», «Аналитика»): новые аварии по интервалам 15 мин / 1 час для GWM_UB и GWM_MB, пики, Excel, экран."""
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import pytest

from test_alarms_analytics import (INDEX, JS, NODE, NSX, STATIC, P, _a, _cat, _ev, _plc, _table, node, read_xlsx)

URL = "/api/alarms/timeline"


async def _setup(pool):
    """GWM_UB, 10.03 (время завода): три аварии в 08:00-08:14, одна в 08:15, одна «активна при старте» в 08:20 (не считается),
    одна в 23:59, одна 11.03 в 00:00. GWM_MB: две аварии 10.03 в 08:05 и одна в 09:30."""
    ub, mb = await _plc(pool, "GWM_UB"), await _plc(pool, "GWM_MB")
    for plc in (ub, mb):
        await _cat(pool, plc, 1, "UB20 sensor blocked", "101", "UB20")
        await _cat(pool, plc, 2, "UB30 motor overload", "102", "UB30")
    for hh, mm, bit in [(8, 0, 1), (8, 7, 2), (8, 14, 1), (8, 15, 2), (23, 59, 1)]:
        await _ev(pool, ub, bit, P("2026-03-10", hh, mm), P("2026-03-10", hh, mm) + timedelta(minutes=1))
    await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at, active_at_start) VALUES ($1, 1, $2, $3, TRUE)",
                       ub, P("2026-03-10", 8, 20), P("2026-03-10", 8, 30))
    await _ev(pool, ub, 2, P("2026-03-11", 0, 0), P("2026-03-11", 0, 5))
    await _ev(pool, mb, 1, P("2026-03-10", 8, 5), P("2026-03-10", 8, 6))
    await _ev(pool, mb, 2, P("2026-03-10", 8, 5), P("2026-03-10", 8, 8))
    await _ev(pool, mb, 1, P("2026-03-10", 9, 30), P("2026-03-10", 9, 40))
    return ub, mb


def _plcs(j):
    return {p["name"]: p for p in j["plcs"]}


def _by_stamp(p):
    return {x["t"]: x["n"] for x in p["series"] if x["n"]}


# ------------------------------------------------------------------ чистая функция
def test_build_timeline_peaks_and_gaps():
    a = _a()
    r = a.build_timeline({0: 2, 5: 7, 6: 7, 95: 1}, date(2026, 3, 10), date(2026, 3, 10), 15)
    assert len(r["series"]) == 96 and r["total"] == 17 and r["max"] == 7
    assert [x["n"] for x in r["peaks"]] == [7, 7, 2]                       # три пика, равные по времени: раньше выше
    assert r["peaks"][0]["t"] < r["peaks"][1]["t"]
    assert sum(1 for x in r["series"] if x["n"] == 0) == 92                # пустые интервалы есть и равны нулю
    assert r["series"][0]["t"] == "2026-03-09T19:00:00Z"                   # начало суток завода UTC+5
    assert a.timeline_stamp(r["peaks"][0]["t"]) == "10.03 01:15"
    e = a.build_timeline({}, date(2026, 3, 10), date(2026, 3, 12), 60)
    assert len(e["series"]) == 72 and e["total"] == 0 and e["max"] == 0 and e["peaks"] == []
    assert a.timeline_summary(e) == "Всего: 0" and a.timeline_peaks_text(e) == ""
    assert a.timeline_summary(r) == "Всего: 17 · максимум: 7 в 10.03 01:15"
    assert a.timeline_peaks_text(r) == "Пики: 10.03 01:15 — 7; 10.03 01:30 — 7; 10.03 00:00 — 2"


# ------------------------------------------------------------------ API
async def test_timeline_buckets_both_plcs_independent(anon, pool):
    await _setup(pool)
    j = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10", "bucket": 15})).json()
    assert [p["name"] for p in j["plcs"]] == ["GWM_UB", "GWM_MB"] and j["bucket"] == 15 and j["offset_hours"] == 5
    ub, mb = _plcs(j)["GWM_UB"], _plcs(j)["GWM_MB"]
    assert len(ub["series"]) == len(mb["series"]) == 96
    # UB: 08:00-08:14 три аварии, 08:15 одна, 23:59 одна; «активна при старте» и 11.03 не входят
    assert _by_stamp(ub) == {"2026-03-10T03:00:00Z": 3, "2026-03-10T03:15:00Z": 1, "2026-03-10T18:45:00Z": 1}
    assert ub["total"] == 5 and ub["max"] == 3 and ub["peaks"][0] == {"t": "2026-03-10T03:00:00Z", "n": 3}
    assert [x["n"] for x in ub["peaks"]] == [3, 1, 1]
    assert _by_stamp(mb) == {"2026-03-10T03:00:00Z": 2, "2026-03-10T04:30:00Z": 1} and mb["total"] == 3
    assert ub["plc_id"] and mb["plc_id"] and ub["plc_id"] != mb["plc_id"]
    h = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10", "bucket": 60})).json()
    ub, mb = _plcs(h)["GWM_UB"], _plcs(h)["GWM_MB"]
    assert len(ub["series"]) == 24 and _by_stamp(ub) == {"2026-03-10T03:00:00Z": 4, "2026-03-10T18:00:00Z": 1}
    assert _by_stamp(mb) == {"2026-03-10T03:00:00Z": 2, "2026-03-10T04:00:00Z": 1}
    assert ub["total"] == 5 and mb["total"] == 3                          # сумма не зависит от интервала


async def test_timeline_period_and_plant_boundaries(anon, pool):
    ub, _ = await _setup(pool)
    await _ev(pool, ub, 1, P("2026-03-09", 23, 59), None)                  # 23:59:00 завода предыдущих суток: вне 10.03
    j = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-11", "bucket": 15})).json()
    p = _plcs(j)["GWM_UB"]
    assert len(p["series"]) == 192 and p["total"] == 6                     # 10.03 и 11.03 00:00 (первый интервал второго дня)
    assert p["series"][96] == {"t": "2026-03-10T19:00:00Z", "n": 1}
    one = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10"})).json()      # интервал по умолчанию 15
    assert one["bucket"] == 15 and _plcs(one)["GWM_UB"]["total"] == 5
    today = (await anon.get(URL)).json()                                     # без параметров: сегодня, ряд без дырок
    assert len(today["plcs"][0]["series"]) == 96


async def test_timeline_exclusions_like_other_numbers(anon, pool):
    ub, mb = await _setup(pool)
    await pool.execute("INSERT INTO alarm_exclusions (plc_id, kind, bit_index) VALUES ($1, 'alarm', 1)", ub)       # бит 1
    await pool.execute("INSERT INTO alarm_exclusions (plc_id, kind, station) VALUES ($1, 'station', 'UB30')", mb)   # станция UB30 (бит 2)
    j = _plcs((await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10"})).json())
    assert j["GWM_UB"]["total"] == 2          # без бита 1 остались бит 2 в 08:07 и 08:15 (авария 11.03 вне периода)
    assert j["GWM_MB"]["total"] == 2          # без станции UB30 (бит 2) остались два события бита 1
    await pool.execute("UPDATE alarm_exclusions SET restored_at = now() WHERE plc_id = $1", mb)
    back = _plcs((await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10"})).json())
    assert back["GWM_MB"]["total"] == 3                                    # возвращённое снова считается


async def test_timeline_validation_and_missing_plc(anon, pool):
    assert (await anon.get(URL, params={"bucket": 30})).status_code == 422
    assert (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-01-01"})).status_code == 400
    assert (await anon.get(URL, params={"from": "2025-01-01", "to": "2026-03-10"})).status_code == 400
    j = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10"})).json()      # ПЛК нет вообще
    assert [p["plc_id"] for p in j["plcs"]] == [None, None] and all(p["total"] == 0 and len(p["series"]) == 96 for p in j["plcs"])


async def test_timeline_found_by_name_not_id(anon, pool):
    await _plc(pool, "Other")
    _, mb = await _setup(pool)
    j = _plcs((await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10"})).json())
    assert j["GWM_MB"]["plc_id"] == mb


# ------------------------------------------------------------------ Excel
def _sheet(z, n):
    sst = [("".join(t.text or "" for t in si.iter(NSX + "t"))) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NSX + "si")]
    out = {}
    for c in ET.fromstring(z.read(f"xl/worksheets/sheet{n}.xml")).iter(NSX + "c"):
        v = c.find(NSX + "v")
        if v is not None:
            out[c.get("r")] = sst[int(v.text)] if c.get("t") == "s" else v.text
    return out


async def test_excel_timeline_matches_api(anon, pool):
    await _setup(pool)
    p = {"from": "2026-03-10", "to": "2026-03-10", "all_hours": "true", "bucket": 15}
    z, rows = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p)).content)
    j = _plcs((await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-10", "bucket": 15})).json())
    a = _a()
    assert any(c.get("A") == f"{a.TIMELINE_TITLE}, интервал 15 мин" for c in rows)
    for name in ("GWM_UB", "GWM_MB"):
        i = next(k for k, c in enumerate(rows) if c.get("A") == name)
        assert rows[i]["B"] == a.timeline_summary(j[name])
        assert rows[i + 1]["B"] == a.timeline_peaks_text(j[name])
    charts = {n: z.read(n).decode("utf-8") for n in z.namelist() if n.startswith("xl/charts/chart")}
    titles = [t for t in (f"{a.TIMELINE_TITLE}, GWM_UB, интервал 15 мин", f"{a.TIMELINE_TITLE}, GWM_MB, интервал 15 мин")]
    for t in titles:
        x = next(v for v in charts.values() if t in v)
        assert "2F80ED" in x                                              # столбцы синие
    assert "C0392B" in next(v for v in charts.values() if titles[0] in v)  # самый большой пик красный
    g = _sheet(z, 2)                                                       # скрытый лист «График»: те же числа, что в API
    for k, name in enumerate(("GWM_UB", "GWM_MB")):
        col, nxt = [("F", "G"), ("I", "J")][k]
        nums = [int(float(g[f"{nxt}{r}"])) for r in range(2, 2 + 96)]
        assert nums == [x["n"] for x in j[name]["series"]]
        assert g[f"{col}2"] == "10.03 00:00" and g[f"{col}97"] == "10.03 23:45"
    p60 = {**p, "bucket": 60}
    z2, rows2 = read_xlsx((await anon.get("/api/alarms/analytics.xlsx", params=p60)).content)
    assert any(c.get("A") == f"{a.TIMELINE_TITLE}, интервал 1 час" for c in rows2)
    assert (await anon.get("/api/alarms/analytics.xlsx", params={**p, "bucket": 7})).status_code == 422


@pytest.mark.skipif(not NODE, reason="нет node")
async def test_excel_timeline_texts_match_screen(anon, pool):
    """Подписи (итог, максимум, пики, время) экран строит тем же текстом, что Excel."""
    await _setup(pool)
    j = (await anon.get(URL, params={"from": "2026-03-10", "to": "2026-03-11", "bucket": 15})).json()
    a = _a()
    for pl in j["plcs"]:
        got = node(f"[u.tlSummary({json.dumps(pl)}, {j['offset_hours']}), u.tlPeaksText({json.dumps(pl)}, {j['offset_hours']})]")
        assert got == [a.timeline_summary(pl), a.timeline_peaks_text(pl)]
    assert node("u.AN_TL_TITLE") == a.TIMELINE_TITLE and node("u.AN_TL_BUCKETS") == {str(k): v for k, v in a.TIMELINE_BUCKETS.items()}


# ------------------------------------------------------------------ экран
@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_timeline_svg():
    def pl(counts, days=1, bucket=15):
        start = datetime(2026, 3, 9, 19, tzinfo=timezone.utc)
        series = [{"t": (start + timedelta(minutes=bucket * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "n": counts.get(i, 0)}
                  for i in range(days * 1440 // bucket)]
        ranked = sorted((x for x in series if x["n"]), key=lambda x: (-x["n"], x["t"]))[:3]
        return {"name": "GWM_UB", "bucket": bucket, "series": series, "total": sum(x["n"] for x in series),
                "max": ranked[0]["n"] if ranked else 0, "peaks": ranked}
    p = pl({40: 215, 41: 30, 60: 7})
    svg = node(f"u.tlSvg({json.dumps(p)}, 900, 5)")
    assert svg.count('class="al-bar al-bar-peak"') == 1 and svg.count('<path class="al-bar"') == 1
    assert svg.count('class="al-ch-peak"') == 1 and svg.count('class="al-ch-val"') == 2     # три подписи пиков: главная красная
    assert "· 215<" in svg and "· 30<" in svg and "· 7<" in svg and 'class="al-ch-hit al-tl-hit"' in svg
    assert "<title" not in svg and "fill:" not in svg and "#" not in svg                  # подсказка одна, цвета только из CSS
    assert node("u.tlSvg({series:[],bucket:15,name:'x',peaks:[]}, 900, 5)") == ""
    # самый длинный период (93 дня по 15 минут): ряд собирается внутри node, чтобы не упереться в длину командной строки
    svg = node("""(() => { const series = []; for (let i = 0; i < 93 * 96; i++) series.push({ t: new Date(Date.UTC(2026, 2, 9, 19, i * 15)).toISOString().replace('.000Z', 'Z'), n: i % 7 });
      const peaks = series.filter(x => x.n === 6).slice(0, 3);
      return u.tlSvg({ name: 'GWM_UB', bucket: 15, series, total: 1, max: 6, peaks }, 900, 5); })()""")
    assert len(svg) < 600_000 and svg.count("<rect") <= 4                                 # 8928 интервалов без 8928 элементов
    empty = node(f"u.tlSvg({json.dumps(pl({}))}, 900, 5)")
    assert "al-bar-peak" not in empty and 'class="al-bar"' not in empty


def test_timeline_screen_static_checks():
    for el in ("al-s-tl", "al-an-tl", "al-tl-seg"):
        assert f'id="{el}"' in JS, el
    assert re.search(r"<h3 title=\"[^\"]{20,}\">\$\{AN_TL_TITLE\}</h3>", JS)
    assert re.search(r'data-tlb="\$\{k\}" title="[^"]{20,}"', JS)                         # у кнопок «15 мин», «1 час» есть подсказки
    assert JS.index("al-s-dyn") < JS.index("al-s-tl") < JS.index("AN_PARETO_TITLE}</h3>")  # после «Динамика по дням», до Парето
    assert "/api/alarms/timeline" in JS and "anBucket: 15" in JS and "bucket: st.anBucket" in JS
    assert "AbortSignal.timeout(6000)" in JS and "refreshTl();" in JS
    css = (STATIC / "alarms.css").read_text(encoding="utf-8")
    assert ".al-bar-peak { fill: var(--danger)" in css and ".al-ch-peak { fill: var(--danger)" in css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.split("/* Аналитика")[1])
