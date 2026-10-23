"""Линия бренда (body_counter): период без галочки «показывать» не виден ни на экране, ни в архиве и Excel,
даже если в нём были кузова; кузова остаются в итоге дня (его даёт ПЛК)."""
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
DAY = date(2026, 10, 5)


def _between(text, start, end):
    i = text.index(start)
    return text[i: text.index(end, i)]


async def _screen(pool, template):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_hid', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('Chery T', $1, $2, '{}'::jsonb) RETURNING id", plc, template)
    for i, fact in enumerate([5, 3, 4], 1):
        await pool.execute(
            "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, $3, $4, $5, 10, $6)",
            scr, DAY, i, 480 + 60 * (i - 1), 540 + 60 * (i - 1), fact)
        await pool.execute(
            "INSERT INTO kpi_schedule (screen_id, idx, end_min, plan, active) VALUES ($1, $2, $3, 10, $4)",
            scr, i, 540 + 60 * (i - 1), i != 2)          # период 2 без галочки, хотя в нём 3 кузова
    return scr


async def test_hourly_api_skips_unchecked_period_even_with_bodies(app, pool, anon):
    scr = await _screen(pool, "body_counter")
    r = await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})
    assert r.status_code == 200
    assert [x["idx"] for x in r.json()["intervals"]] == [1, 3]


async def test_fl_screen_keeps_period_with_bodies(app, pool, anon):
    """У Finish Line план даёт ПЛК: интервал с кузовами скрывать нельзя, итоги разойдутся (прежнее правило)."""
    scr = await _screen(pool, "fl_counter")
    r = await anon.get(f"/api/kpi/screens/{scr}/hourly", params={"date": DAY.isoformat()})
    assert [x["idx"] for x in r.json()["intervals"]] == [1, 2, 3]


def test_brand_screen_row_hidden_when_unchecked():
    paint = _between(HTML, "const built = `${screen.id}|${n}|${andonCfgVer}`;", "paintTakt(screen")
    body = HTML[HTML.index("paintTakt(screen, val('curMin')"):]
    body = body[: body.index("tr.hidden") + 80]
    assert "tr.hidden = !cfg.active;" in body
    assert paint
