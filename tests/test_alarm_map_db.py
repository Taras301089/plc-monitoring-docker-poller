"""Карта аварий в базе (S-14): таблица alarm_source_map, версии, alarm_source() из таблицы и из файла, API, запись при сборке «Файла для ИИ»."""
import asyncio
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import alarm_map
import alarm_map_db as db
import tia
import tia_logic
from test_alarm_map import two_stations

FIX = Path(__file__).resolve().parent / "fixtures" / "tia_logic" / "PLC1" / "Blocks"


async def _plc(pool, name="PLC_MAP"):
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.1.2.3:4840', TRUE) RETURNING id", name)


def _data(built_from="TIA_p_1.zip", catalog=None):
    return alarm_map.build_map(two_stations(), catalog or {}, built_from)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    (tmp_path / "logic").mkdir()
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(tia, "_spawn", lambda fn, *a: fn(*a))
    return tmp_path


async def test_schema_and_idempotent(app, pool):
    cols = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'alarm_source_map'")}
    assert set(db.COLS) | {"id"} == cols
    await db.ensure_alarm_map_schema(pool)
    await db.ensure_alarm_map_schema(pool)
    idx = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE tablename = 'alarm_source_map' AND indexname = 'alarm_source_map_bit_idx'")
    assert "(plc_id, bit_index, built_at)" in idx
    p = await _plc(pool)
    with pytest.raises(Exception):
        await pool.execute("INSERT INTO alarm_source_map (plc_id, bit_index, matched_by) VALUES ($1, 1, 'guess')", p)
    assert db.bound_pool() is app.state.pool                                       # старт сайта привязал базу к сборке «Файла для ИИ»


async def test_save_and_read_latest_version(app, pool):
    p = await _plc(pool)
    n = await db.save_map(pool, p, _data(), "TIA_p_1.zip")
    assert n == len(_data()["alarms"])
    r = await db.alarm_source(pool, p, 160)
    assert (r["found"], r["from"], r["block"], r["net"], r["msg_type"], r["station_no"], r["matched_by"]) == (True, "db", "ObjFb", 2, "Sonser", "1", "order")
    assert (r["word_index"], r["bit_in_word"], r["latched"], r["built_from"], r["stale"]) == (10, 0, True, "TIA_p_1.zip", False)
    assert r["source_expr"].startswith("ST1_DB.OBJ1.Fault: ") and "Operator.Reset" in r["reset_expr"] and "Main (сеть 1)" in r["call_path"]
    assert (await db.alarm_source(pool, p, 801))["station_no"] == "Ячейка 4"
    assert (await db.alarm_source(pool, p, 5)) == {"found": False, "plc_id": p, "bit_index": 5, "stale": False}
    assert (await db.alarm_source(pool, p + 100, 160))["found"] is False


async def test_same_export_replaces_its_version_and_old_versions_are_cut(app, pool):
    p = await _plc(pool)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    d = _data()
    await db.save_map(pool, p, d, "TIA_p_1.zip", base)
    await db.save_map(pool, p, d, "TIA_p_1.zip", base + timedelta(minutes=1))               # та же выгрузка: версия заменяется
    assert await pool.fetchval("SELECT count(*) FROM alarm_source_map WHERE plc_id = $1", p) == len(d["alarms"])
    for i in range(2, 8):
        await db.save_map(pool, p, d, f"TIA_p_{i}.zip", base + timedelta(hours=i))
    versions = [r["built_from"] for r in await pool.fetch(
        "SELECT built_from FROM alarm_source_map WHERE plc_id = $1 GROUP BY built_from ORDER BY max(built_at)", p)]
    assert versions == [f"TIA_p_{i}.zip" for i in range(3, 8)]                              # пять последних, старше удалены
    assert (await db.alarm_source(pool, p, 160))["built_from"] == "TIA_p_7.zip"


async def test_bit_missing_in_latest_version_is_not_taken_from_old_one(app, pool):
    p = await _plc(pool)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await db.save_map(pool, p, _data("old"), "old", base)
    small = _data("new")
    small["alarms"].pop("160")
    await db.save_map(pool, p, small, "new", base + timedelta(days=1))
    assert (await db.alarm_source(pool, p, 160))["found"] is False
    assert (await db.alarm_source(pool, p, 161))["built_from"] == "new"


def _write_file(bridge: Path, plc_id: int, data: dict, project="gwm_ub", built_at=None):
    data["meta"].update({"plc_id": plc_id, "built_at": (built_at or datetime.now(timezone.utc)).isoformat(), "built_from": data["meta"]["built_from"]})
    f = bridge / "logic" / f"{project}.alarms.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f


async def test_file_is_used_when_table_or_row_is_missing(app, pool, bridge):
    p = await _plc(pool)
    _write_file(bridge, p, _data("TIA_f.zip"))
    r = await db.alarm_source(pool, p, 160)                                                   # строки в таблице нет
    assert (r["found"], r["from"], r["built_from"], r["block"], r["station_no"]) == (True, "file", "TIA_f.zip", "ObjFb", "1")
    assert r["chain"] and r["flags"] == [] and r["net_title"] == "Fault"
    assert (await db.alarm_source(None, p, 160))["from"] == "file"                            # базы нет совсем
    await pool.execute("DROP TABLE alarm_source_map")                                         # таблицы нет (на рабочей базе до выкладки)
    try:
        assert (await db.alarm_source(pool, p, 161))["from"] == "file"
    finally:
        await db.ensure_alarm_map_schema(pool)
    assert (await db.alarm_source(pool, p, 99999))["found"] is False and (await db.alarm_source(pool, p + 1, 160))["found"] is False


async def test_stale_when_logic_file_is_newer_than_map(app, pool, bridge):
    p = await _plc(pool)
    logic = bridge / "logic" / "gwm_ub.json"
    logic.write_text("{}", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_file(bridge, p, _data("TIA_s.zip"), built_at=old)
    await db.save_map(pool, p, _data("TIA_s.zip"), "TIA_s.zip", old)
    assert (await db.alarm_source(pool, p, 160))["stale"] is True and (await db.alarm_source(pool, p, 160))["from"] == "db"
    await pool.execute("DELETE FROM alarm_source_map")
    assert (await db.alarm_source(pool, p, 160))["stale"] is True                             # то же из файла
    fresh = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.save_map(pool, p, _data("TIA_s.zip"), "TIA_s.zip", fresh)
    assert (await db.alarm_source(pool, p, 160))["stale"] is False


async def test_api_requires_login_and_returns_source(app, users, anon, pool):
    p = await _plc(pool)
    await db.save_map(pool, p, _data(), "TIA_p_1.zip")
    assert (await anon.get("/api/alarm-map", params={"plc_id": p, "bit": 160})).status_code == 401
    for login in ("t_viewer", "t_master", "t_admin"):
        r = await users[login].get("/api/alarm-map", params={"plc_id": p, "bit": 160})
        assert r.status_code == 200 and r.json()["found"] is True and r.json()["block"] == "ObjFb"
    assert (await users["t_viewer"].get("/api/alarm-map", params={"plc_id": p, "bit": 5})).json()["found"] is False
    assert (await users["t_viewer"].get("/api/alarm-map", params={"plc_id": p})).status_code == 422
    assert (await users["t_viewer"].get("/api/alarm-map", params={"plc_id": p, "bit": -1})).status_code == 422


async def test_build_writes_file_and_table_with_catalog_check(app, pool, bridge):
    p = await _plc(pool, "PLC_B")
    await pool.execute("INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, text, is_placeholder) VALUES ($1, 800, 50, 0, 'Air pressure low', FALSE)", p)
    (bridge / "tia-projects.json").write_text(json.dumps({"projects": [{"id": "gwm_ub", "name": "x", "path": "C:/x.ap17", "plc": "PLC_B"}]}), encoding="utf-8")
    out = bridge / "logic" / "gwm_ub.json"
    res = await asyncio.to_thread(tia._build_alarm_map, "gwm_ub", "TIA_gwm_ub_2026-10-22_10-00.zip", two_stations(), out)
    assert res["state"] == "ready" and res["file"] == "gwm_ub.alarms.json" and res["confirmed"] == 1 and res["with_text"] == 1 and "db_error" not in res
    f = json.loads((bridge / "logic" / "gwm_ub.alarms.json").read_text(encoding="utf-8"))
    assert f["meta"]["plc_id"] == p and f["meta"]["built_from"] == "TIA_gwm_ub_2026-10-22_10-00.zip" and f["alarms"]["800"]["by"] == "text"
    assert res["db_rows"] == len(f["alarms"]) and await pool.fetchval("SELECT count(*) FROM alarm_source_map WHERE plc_id = $1", p) == res["db_rows"]
    assert (await db.alarm_source(pool, p, 800))["matched_by"] == "text"


async def test_build_survives_missing_table_and_missing_main(app, pool, bridge):
    p = await _plc(pool, "PLC_C")
    (bridge / "tia-projects.json").write_text(json.dumps({"projects": [{"id": "gwm_ub", "name": "x", "path": "C:/x.ap17", "plc": "PLC_C"}]}), encoding="utf-8")
    out = bridge / "logic" / "gwm_ub.json"
    await pool.execute("DROP TABLE alarm_source_map")
    try:
        res = await asyncio.to_thread(tia._build_alarm_map, "gwm_ub", "TIA_gwm_ub_2026-10-22_11-00.zip", two_stations(), out)
    finally:
        await db.ensure_alarm_map_schema(pool)
    assert res["state"] == "ready" and "db_error" in res and (bridge / "logic" / "gwm_ub.alarms.json").is_file()   # файл карты есть, ошибка в состоянии
    bad = await asyncio.to_thread(tia._build_alarm_map, "gwm_ub", "TIA_gwm_ub_2026-10-22_12-00.zip", tia_logic.TiaLogic({}, "P"), out)
    assert bad["state"] == "error" and "Main" in bad["error"]


async def test_logic_build_keeps_main_json_when_map_fails(app, bridge):
    """Сборка «Файла для ИИ» на выгрузке без блока Main: основной JSON записан, карта в состоянии «ошибка»."""
    zname = "TIA_wbs1_2026-10-19_16-10.zip"
    with zipfile.ZipFile(bridge / "out" / zname, "w") as z:
        for f in FIX.glob("*.xml"):
            z.write(f, f"2026-10-19_16-10/PLC1/Blocks/{f.name}")
    await asyncio.to_thread(tia.build_logic, "wbs1", zname)
    st = json.loads((bridge / "logic" / "wbs1.status.json").read_text(encoding="utf-8"))
    assert st["state"] == "ready" and st["alarms"]["state"] == "error"
    assert json.loads((bridge / "logic" / "wbs1.json").read_text(encoding="utf-8"))["blocks"]
    assert not (bridge / "logic" / "wbs1.alarms.json").exists()
    assert tia._logic_state("wbs1", {"file": zname})["alarms"]["state"] == "error"
