"""«Аварии: что делать» (S-12, часть 2): схема, права, история, уникальность, alarm_help_for(), Excel = экран, заготовки."""
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EDITORS = ["t_admin", "t_chief", "t_area", "t_master", "t_oto"]
FULL = {"simple_description": "Не пришёл сигнал от датчика", "typical_causes": "1. Обрыв кабеля\n2. Загрязнён датчик", "fix_steps": "1. Осмотреть датчик\n2. Позвать КИП"}


def _ah():
    import alarmhelp
    return alarmhelp


async def _plc(pool, name="PLC_AH"):
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.1.2.3:4840', TRUE) RETURNING id", name)


async def _cat(pool, plc, bit, text, code=""):
    await pool.execute(
        "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, code, text, class, is_placeholder, station, device, parsed) "
        "VALUES ($1, $2, 0, $3, $4, $5, 'Alarm', FALSE, '', '', TRUE)", plc, bit, bit % 16, code, text)


async def _add(client, plc, bit, **kw):
    r = await client.post("/api/alarm-help", json={"plc_id": plc, "bit_index": bit, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_schema_and_idempotent(app, pool):
    cols = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'alarm_help'")}
    assert {"plc_id", "bit_index", "alarm_text_src", "simple_description", "typical_causes", "fix_steps", "status", "updated_by_name", "updated_at"} <= cols
    log = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'alarm_help_log'")}
    assert {"help_id", "plc_id", "bit_index", "action", "was", "now", "by_name", "at"} <= log
    await _ah().ensure_alarm_help_schema(pool)
    await _ah().ensure_alarm_help_schema(pool)
    p = await _plc(pool)
    with pytest.raises(Exception):
        await pool.execute("INSERT INTO alarm_help (plc_id, bit_index, status) VALUES ($1, 1, 'bad')", p)


async def test_alarm_help_for(app, users, pool):
    p = await _plc(pool)
    c = users["t_master"]
    await _add(c, p, 5)
    sid = await _add(c, p, 6, **FULL)
    f = _ah().alarm_help_for
    assert await f(pool, p, 5) is None                      # ничего не заполнено
    assert await f(pool, p, 99) is None and await f(pool, p + 1, 6) is None
    assert await f(pool, p, 6) == {"simple": FULL["simple_description"], "causes": FULL["typical_causes"], "fix": FULL["fix_steps"]}
    assert await f(pool, p, 6, only_checked=True) is None
    assert (await c.put(f"/api/alarm-help/{sid}", json={**FULL, "status": "checked"})).status_code == 200
    assert await f(pool, p, 6, only_checked=True) is not None


async def test_access(app, users, anon, pool):
    p = await _plc(pool)
    hid = await _add(users["t_admin"], p, 1)
    assert (await anon.get("/api/alarm-help")).status_code == 401
    assert (await anon.get("/api/alarm-help.xlsx")).status_code == 401
    v = users["t_viewer"]
    assert (await v.get("/api/alarm-help")).status_code == 200
    assert (await v.post("/api/alarm-help", json={"plc_id": p, "bit_index": 2})).status_code == 403
    assert (await v.put(f"/api/alarm-help/{hid}", json={})).status_code == 403
    assert (await v.post("/api/alarm-help/drafts", json={"plc_id": p})).status_code == 403
    assert (await v.delete(f"/api/alarm-help/{hid}")).status_code == 403


@pytest.mark.parametrize("login", EDITORS)
async def test_editors_can_write(app, users, pool, login):
    p = await _plc(pool)
    hid = await _add(users[login], p, 1)
    assert (await users[login].put(f"/api/alarm-help/{hid}", json=FULL)).json()["changed"] is True


async def test_delete_only_author_and_admin(app, users, pool):
    p = await _plc(pool)
    hid = await _add(users["t_master"], p, 1)
    assert (await users["t_oto"].delete(f"/api/alarm-help/{hid}")).status_code == 403
    assert (await users["t_chief"].delete(f"/api/alarm-help/{hid}")).status_code == 403
    assert (await users["t_master"].delete(f"/api/alarm-help/{hid}")).status_code == 200
    h2 = await _add(users["t_master"], p, 2)
    assert (await users["t_admin"].delete(f"/api/alarm-help/{h2}")).status_code == 200
    assert (await users["t_admin"].delete(f"/api/alarm-help/{h2}")).status_code == 404


async def test_unique_validation_and_catalog_text(app, users, pool):
    p1, p2 = await _plc(pool, "A1"), await _plc(pool, "A2")
    await _cat(pool, p1, 7, "Clamp timeout", "E007")
    c = users["t_master"]
    await _add(c, p1, 7)
    it = (await c.get("/api/alarm-help", params={"plc_id": p1})).json()["items"][0]
    assert it["alarm_text_src"] == "E007 Clamp timeout" and it["bit_index"] == 7 and it["author"] == "T_MASTER, Мастер"
    assert (await c.post("/api/alarm-help", json={"plc_id": p1, "bit_index": 7})).status_code == 409
    await _add(c, p2, 7)                                    # другой ПЛК допустим
    assert (await c.post("/api/alarm-help", json={"plc_id": 99999, "bit_index": 1})).status_code == 404
    assert (await c.post("/api/alarm-help", json={"plc_id": p1, "bit_index": -1})).status_code == 422
    assert (await c.post("/api/alarm-help", json={"plc_id": p1, "bit_index": 8, "status": "zzz"})).status_code == 422
    assert (await c.post("/api/alarm-help", json={"plc_id": p1, "bit_index": 8, "status": "checked", "simple_description": "x"})).status_code == 422


async def test_multiline_kept_and_history(app, users, pool):
    p = await _plc(pool)
    m, a = users["t_master"], users["t_admin"]
    hid = await _add(m, p, 3)
    await m.put(f"/api/alarm-help/{hid}", json=FULL)
    await m.put(f"/api/alarm-help/{hid}", json=FULL)       # без изменений: записи нет
    await a.put(f"/api/alarm-help/{hid}", json={**FULL, "status": "checked"})
    assert (await m.get("/api/alarm-help")).json()["items"][0]["typical_causes"] == FULL["typical_causes"]
    log = (await m.get(f"/api/alarm-help/{hid}/log")).json()["items"]
    assert [x["action"] for x in log] == ["create", "update", "update"]
    assert log[1]["was"]["fix_steps"] == "" and log[1]["now"]["fix_steps"] == FULL["fix_steps"] and log[1]["by"].startswith("T_MASTER")
    assert log[2]["now"]["status"] == "checked" and log[2]["by"].startswith("T_ADMIN")
    await a.delete(f"/api/alarm-help/{hid}")
    acts = [r["action"] for r in await pool.fetch("SELECT action FROM alarm_help_log WHERE help_id = $1 ORDER BY id", hid)]
    assert acts == ["create", "update", "update", "delete"]


async def test_filters(app, users, pool):
    p1, p2 = await _plc(pool, "A1"), await _plc(pool, "A2")
    c = users["t_master"]
    await _add(c, p1, 1, simple_description="Лифт не дошёл", fix_steps="Проверить датчик")
    await _add(c, p1, 2, alarm_text_src="Clamp open timeout")
    await _add(c, p2, 30, typical_causes="шкаф 7")

    async def bits(q):
        return [i["bit_index"] for i in (await c.get("/api/alarm-help", params=q)).json()["items"]]
    assert await bits({}) == [1, 2, 30]
    assert await bits({"plc_id": p1}) == [1, 2]
    assert await bits({"q": "лифт"}) == [1] and await bits({"q": "CLAMP"}) == [2] and await bits({"q": "шкаф"}) == [30] and await bits({"q": "30"}) == [30]
    assert await bits({"status": "checked"}) == []
    assert (await c.get("/api/alarm-help", params={"status": "zzz"})).status_code == 422


def _sheet(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    sst = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            v = c.find(NS + "v")
            if v is not None:
                cells[re.match(r"[A-Z]+", c.get("r")).group(0)] = sst[int(v.text)] if c.get("t") == "s" else v.text
        rows.append([cells.get(ch, "") for ch in "ABCDEFGH"])
    return rows


async def test_excel_matches_screen(app, users, pool):
    p1, p2 = await _plc(pool, "A1"), await _plc(pool, "A2")
    c = users["t_master"]
    await _add(c, p1, 4, alarm_text_src="Body timeout", **FULL)
    await _add(c, p2, 9)
    js = (STATIC / "alarmhelp.js").read_text(encoding="utf-8")
    titles = re.findall(r"\{ k: '(?!act)\w+', t: '([^']+)'", js)
    r = await c.get("/api/alarm-help.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert unquote(re.search(r"filename\*=UTF-8''(.+)", r.headers["content-disposition"]).group(1)).startswith("Справка по авариям_все ПЛК_")
    rows = _sheet(r.content)
    header = next(x for x in rows if x[0] == "ПЛК")
    assert header == titles == [t for _k, t in _ah().COLUMNS]
    items = (await c.get("/api/alarm-help")).json()["items"]
    assert rows[rows.index(header) + 1:] == [[i["plc"], str(i["bit_index"]), i["alarm_text_src"], i["simple_description"], i["typical_causes"],
                                              i["fix_steps"], i["status_name"], i["author"]] for i in items]
    r2 = await c.get("/api/alarm-help.xlsx", params={"plc_id": p2})
    assert [x[1] for x in _sheet(r2.content) if x[0] == "A2"] == ["9"]


def test_screen_is_wired():
    js = (STATIC / "alarmhelp.js").read_text(encoding="utf-8")
    assert "attachColWidths" in js and "alarmHelpSection" in js and "/api/alarm-help" in js
    assert 'src="/static/alarmhelp.js"' in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "dict-alarmhelp" in (STATIC / "dictionary.js").read_text(encoding="utf-8")
    for t in re.findall(r'title="([^"]{3,})"', js) + re.findall(r"tip: '([^']+)'", js):
        if "${" not in t[:12]:
            assert 10 <= len(t.split()) <= 26, t
    assert not re.search(r"font-size:\s*\d+px", js)


async def test_drafts_top_by_count_last_7_days(app, users, pool):
    p = await _plc(pool, "GWM_UB")
    now = datetime.now(timezone.utc)
    for bit, text, code in [(1, "Body check timeout", "A001"), (2, "Lift limit", "A002"), (3, "Old alarm", "A003"), (4, "Third", "A004")]:
        await _cat(pool, p, bit, text, code)
    for bit, n, ago in [(1, 5, 1), (2, 2, 2), (3, 9, 30), (4, 1, 1)]:
        for k in range(n):
            await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at) VALUES ($1, $2, $3, $4)",
                               p, bit, now - timedelta(days=ago, minutes=k * 10), now - timedelta(days=ago, minutes=k * 10 - 1))
    c = users["t_oto"]
    r = await c.post("/api/alarm-help/drafts", json={"plc_id": p, "limit": 2, "days": 7})
    assert r.json() == {"added": 2, "skipped": 0, "found": 2}
    items = (await c.get("/api/alarm-help", params={"plc_id": p})).json()["items"]
    assert {i["bit_index"]: i["alarm_text_src"] for i in items} == {1: "A001 Body check timeout", 2: "A002 Lift limit"}
    assert all(i["status"] == "draft" and not i["simple_description"] and not i["typical_causes"] and not i["fix_steps"] for i in items)
    # ручная правка не затирается повторной заготовкой; авария 3 старше 7 дней не берётся
    await c.put(f"/api/alarm-help/{items[0]['id']}", json={**FULL, "alarm_text_src": items[0]["alarm_text_src"]})
    r = await c.post("/api/alarm-help/drafts", json={"plc_id": p, "limit": 20, "days": 7})
    assert r.json() == {"added": 1, "skipped": 2, "found": 3}
    assert (await c.post("/api/alarm-help/drafts", json={"plc_id": 99999})).status_code == 404
