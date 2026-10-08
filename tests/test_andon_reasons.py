"""Андон: название причины и описание простоя одинаковы для всех (табло без входа, мастер, администратор)."""
import datetime
import re
from pathlib import Path

DT_JS = Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "downtime.js"


async def _seed(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    reason = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_Причина_новая', '', 9999) RETURNING id")
    dt = await pool.fetchval(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) "
        "VALUES ($1, CURRENT_DATE, 1, 60, 0, 60, 60) RETURNING id", scr)
    await pool.execute("INSERT INTO kpi_downtime_items (downtime_id, reason_id, minutes, note) VALUES ($1, $2, 60, 't_описание')", dt, reason)
    return scr


async def test_andon_data_same_for_everyone(app, users, anon, pool):
    scr = await _seed(pool)
    results = {}
    for name, c in {"anon": anon, "master": users["t_master"], "admin": users["t_admin"]}.items():
        d = await c.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": datetime.date.today().isoformat()})
        r = await c.get("/api/kpi/dictionary")
        assert d.status_code == 200 and r.status_code == 200, name
        item = d.json()["downtimes"][0]["items"][0]
        names = {x["id"]: x["name"] for x in r.json()["reasons"]}
        results[name] = (names.get(item["reason_id"]), item["note"])
    assert results["anon"] == results["master"] == results["admin"] == ("t_Причина_новая", "t_описание")


def test_andon_refreshes_dictionary():
    """Табло открыто сутками: справочник причин должен перечитываться, а не грузиться один раз (иначе новая причина показывается как «—»)."""
    js = DT_JS.read_text(encoding="utf-8")
    body = js[js.index("window.dtApply"): js.index("function fitWhy")]
    assert "dictAt" in body, "dtApply обязан перечитывать справочник по давности (dictAt), а не только при !dict"
    assert not re.search(r"if \(!dict\) loadDict", body)
