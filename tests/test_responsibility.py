"""Ответственность за отставание, шаг 1: «Ответственный» (ПР / ОТО / ИТО / ДР) у причины простоя, статус, Andon, «Простои», Excel."""
import re
from datetime import date
from pathlib import Path

import pytest

import dayflags
import downtime
import history
import dictionary

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
DAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dayflags, "plant_today", lambda: date(2026, 10, 12))


async def _seed(pool, reason_owner="production"):
    """Линия GWM, интервал 1 с отставанием 30 мин, две причины."""
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_plc', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval("INSERT INTO kpi_screens (name, plc_id) VALUES ('GWM', $1) RETURNING id", plc)
    await pool.execute(
        "INSERT INTO kpi_hourly (screen_id, prod_date, idx, start_min, end_min, plan, fact) VALUES ($1, $2, 1, 480, 540, 10, 5)", scr, DAY)
    dt = await pool.fetchval(
        "INSERT INTO kpi_downtimes (screen_id, prod_date, idx, plan, fact, interval_min, minutes) VALUES ($1, $2, 1, 10, 5, 60, 30) RETURNING id",
        scr, DAY)
    r_def = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order, owner) VALUES ('t_default', '', 9001, $1) RETURNING id", reason_owner)
    r_oto = await pool.fetchval("INSERT INTO kpi_reasons (name, hint, sort_order, owner) VALUES ('t_oto_reason', '', 9002, 'oto') RETURNING id")
    return scr, dt, r_def, r_oto


def _item(reason_id, minutes=10, **kw):
    return {"reason_id": reason_id, "minutes": minutes, "note": kw.pop("note", ""), **kw}


async def _put(client, dt, items):
    return await client.put(f"/api/kpi/downtimes/{dt}/items", json={"items": items})


async def _items(client, scr):
    r = await client.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": DAY.isoformat()})
    assert r.status_code == 200
    return r.json()["downtimes"][0]["items"]


# ---------- схема ----------

async def test_schema_idempotent_and_default_production(app, pool):
    for table in ("kpi_reasons", "kpi_downtime_items"):
        col = await pool.fetchrow(
            "SELECT column_default, is_nullable FROM information_schema.columns WHERE table_name = $1 AND column_name = 'owner'", table)
        assert col is not None and col["is_nullable"] == "NO" and "production" in col["column_default"], table
    # старые записи (до появления поля) получают «production», ничьи значения не меняются
    await pool.execute("ALTER TABLE kpi_reasons DROP COLUMN owner")
    await pool.execute("INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ('t_old', '', 1)")
    for _ in range(2):          # повторный запуск безопасен
        await dictionary.ensure_dictionary_schema(pool)
        await downtime.ensure_downtime_schema(pool)
    assert await pool.fetchval("SELECT owner FROM kpi_reasons WHERE name = 't_old'") == "production"
    assert await pool.fetchval("SELECT to_regclass('kpi_downtime_items_owner_log')") is not None


# ---------- справочник причин ----------

async def test_dictionary_has_default_owner(app, users, anon, pool):
    await pool.execute("INSERT INTO kpi_reasons (name, hint, sort_order, owner) VALUES ('t_r', '', 5, 'ito')")
    r = await anon.get("/api/kpi/dictionary")
    assert next(x for x in r.json()["reasons"] if x["name"] == "t_r")["owner"] == "ito"


async def test_reason_owner_edit_rights_and_validation(app, users, pool):
    admin = users["t_admin"]
    rid = (await admin.post("/api/kpi/reasons", json={"name": "t_new", "owner": "oto"})).json()["id"]
    assert await pool.fetchval("SELECT owner FROM kpi_reasons WHERE id = $1", rid) == "oto"
    # правка без поля «owner» значение не меняет
    assert (await admin.put(f"/api/kpi/reasons/{rid}", json={"name": "t_new2"})).status_code == 200
    assert await pool.fetchval("SELECT owner FROM kpi_reasons WHERE id = $1", rid) == "oto"
    assert (await admin.put(f"/api/kpi/reasons/{rid}", json={"name": "t_new2", "owner": "other"})).status_code == 200
    assert await pool.fetchval("SELECT owner FROM kpi_reasons WHERE id = $1", rid) == "other"
    assert (await admin.put(f"/api/kpi/reasons/{rid}", json={"name": "t_new2", "owner": "boss"})).status_code == 422
    assert (await users["t_master"].put(f"/api/kpi/reasons/{rid}", json={"name": "x", "owner": "ito"})).status_code == 403


# ---------- описание простоя ----------

async def test_owner_taken_from_reason_and_can_be_changed(app, users, pool):
    scr, dt, r_def, r_oto = await _seed(pool)
    m = users["t_master"]
    assert (await _put(m, dt, [_item(r_oto, 10), _item(r_def, 10, owner="oto")])).status_code == 200
    its = {i["reason_id"]: i for i in await _items(m, scr)}
    assert its[r_oto]["owner"] == "oto"          # не указан: подставился из причины
    assert its[r_def]["owner"] == "oto"          # указан вручную: причина «production», выбрано «oto»


async def test_status_by_owner(app, users, pool):
    scr, dt, r_def, r_oto = await _seed(pool)
    m = users["t_master"]
    assert (await _put(m, dt, [_item(r_def, 5, owner="production"), _item(r_def, 5, owner="oto"), _item(r_def, 5, owner="ito"),
                               _item(r_def, 5, owner="other", note="нет электричества")])).status_code == 200
    got = [(i["owner"], i["owner_status"]) for i in await _items(m, scr)]
    assert got == [("production", "confirmed"), ("oto", "pending"), ("ito", "pending"), ("other", "confirmed")]
    assert downtime.owner_status("oto") == "pending" and downtime.owner_status("other") == "confirmed"


async def test_other_requires_description_on_server(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    m = users["t_master"]
    r = await _put(m, dt, [_item(r_def, 10, owner="other", note="   ")])
    assert r.status_code == 422 and "ДР" in r.json()["detail"]
    assert (await _put(m, dt, [_item(r_def, 10, owner="other", note="поставщик не привёз")])).status_code == 200
    assert (await _put(m, dt, [_item(r_def, 10, owner="boss")])).status_code == 422


async def test_owner_unspecified_keeps_existing_on_update(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    m = users["t_master"]
    await _put(m, dt, [_item(r_def, 10, owner="ito")])
    iid = (await _items(m, scr))[0]["id"]
    assert (await _put(m, dt, [_item(r_def, 12, id=iid)])).status_code == 200      # клиент поле не прислал
    assert (await _items(m, scr))[0]["owner"] == "ito"


async def test_non_author_cannot_change_owner(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="production")])
    it = (await _items(users["t_master"], scr))[0]
    same = _item(it["reason_id"], 10, id=it["id"], owner="oto")
    r = await _put(users["t_oto"], dt, [same])
    assert r.status_code == 403
    assert (await _items(users["t_master"], scr))[0]["owner"] == "production"
    # начальник (модератор) может менять
    assert (await _put(users["t_chief"], dt, [same])).status_code == 200
    assert (await _items(users["t_master"], scr))[0]["owner"] == "oto"


async def test_owner_change_is_logged(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    m = users["t_master"]
    await _put(m, dt, [_item(r_def, 10, owner="production")])
    iid = (await _items(m, scr))[0]["id"]
    await _put(m, dt, [_item(r_def, 10, id=iid, owner="oto")])
    await _put(m, dt, [_item(r_def, 10, id=iid, owner="oto")])       # без изменений: новой записи нет
    rows = await pool.fetch("SELECT old_owner, new_owner, changed_by_name FROM kpi_downtime_items_owner_log WHERE item_id = $1 ORDER BY id", iid)
    assert [(r["old_owner"], r["new_owner"]) for r in rows] == [(None, "production"), ("production", "oto")]
    assert rows[1]["changed_by_name"]


# ---------- «Простои» и Excel ----------

async def test_downtimes_list_has_owner(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="ito")])
    r = await anon.get("/api/kpi/downtimes-list", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    row = next(x for x in r.json()["rows"] if x["kind"] == "item")
    assert row["owner"] == "ito" and row["owner_status"] == "pending"


def test_owner_label_is_one_function():
    assert downtime.owner_label("production", "confirmed") == "ПР ✓"
    assert downtime.owner_label("oto", "pending") == "ОТО …"
    assert downtime.owner_label("ito", "pending") == "ИТО …"
    assert downtime.owner_label("other", "confirmed") == "ДР ✓"
    assert downtime.owner_label("oto", "disputed") == "ОТО !"      # статус появится на шаге 2


async def test_excel_day_has_owner_column_like_screen(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="oto"), _item(r_def, 10, owner="production")])
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    assert "Отв." in pv["headers"]
    c = pv["headers"].index("Отв.")
    assert c == pv["headers"].index("Простой мин") + 1   # как на экране Andon: «Отв.» сразу после «Простой, мин»
    assert [r[c] for r in pv["rows"]] == ["ОТО …", "ПР ✓", ""]       # две причины и «не описан» (пусто)
    assert len(pv["headers"]) == len(pv["rows"][0])
    xl = await anon.get(f"/api/kpi/screens/{scr}/day.xlsx", params={"date": DAY.isoformat()})
    assert xl.status_code == 200
    assert "Отв.".encode() in _sheet_strings(xl.content)


def _sheet_strings(content: bytes) -> bytes:
    import io
    import zipfile
    return zipfile.ZipFile(io.BytesIO(content)).read("xl/sharedStrings.xml")


async def test_period_excel_list_has_owner(app, users, anon, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10, owner="ito")])
    xl = await anon.get(f"/api/kpi/screens/{scr}/period.xlsx", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    assert xl.status_code == 200
    s = _sheet_strings(xl.content).decode("utf-8")
    assert "Отв." in s and "ИТО …" in s


# ---------- интерфейс (исходники страниц) ----------

def _src(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_andon_tables_have_owner_column_after_downtime_column():
    for name in ("index.html", "manual.js"):
        s = _src(name)
        assert 'data-k="own"' in s and ">Отв.<" in s, name
        # колонка стоит сразу после колонки с минутами и перед «Причина»
        assert re.search(r'data-k="dt"></td><td class="own" data-k="own"></td><td class="why"', s), name
    assert _src("index.html").count('data-k="own"') == 2          # Main Line и Finish Line


def test_downtime_js_fills_owner_and_dialog_has_select():
    js = _src("downtime.js")
    assert "data-f=\"owner\"" in js and "production" in js and "'other'" in js
    assert "[data-k=\"own\"]" in js
    # подстановка из причины и обязательное описание у ДР (клиентская проверка)
    assert "owner" in js[js.index("on('reason'"): js.index("on('minutes'")]
    assert "ДР" in js


def test_downtimes_screen_has_owner_column():
    s = _src("downtimes.js")
    assert "k: 'owner'" in s and "'Отв.'" in s


def test_dictionary_editor_has_default_owner_select():
    s = _src("dictionary.js")
    assert "owner" in s and "Ответственный по умолчанию" in s


def test_owner_dropdown_is_like_other_fields():
    """«Ответственный отдел» устроен как «Станция…» и «Причина…»: подсказка внутри поля, выбор из списка, без подписи над полями."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "downtime.js").read_text(encoding="utf-8")
    assert '<option value="">Ответственный отдел…</option>' in js
    assert "owner: ''" in js and "owner: 'production' }" not in js     # новая строка начинается без выбранного отдела
    assert "выберите ответственный отдел" in js                        # сохранить без отдела нельзя
    assert "dt-head" not in js
