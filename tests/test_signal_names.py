"""Простые названия сигналов (S-12): схема, права, история, уникальность, friendly(), нормализация, Excel = экран, заготовки."""
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
FIX = Path(__file__).resolve().parent / "fixtures" / "tia_logic"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EDITORS = ["t_admin", "t_chief", "t_area", "t_master", "t_oto"]


def _sn():
    import signalnames
    return signalnames


async def _plc(pool, name="PLC_S12"):
    return await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.1.2.3:4840', TRUE) RETURNING id", name)


async def _add(client, plc, tag, **kw):
    r = await client.post("/api/signal-names", json={"plc_id": plc, "tag": tag, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- схема ----------

async def test_schema_columns_and_constraints(app, pool):
    cols = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'signal_names'")}
    assert {"id", "plc_id", "tag", "tag_key", "simple_name", "place", "status", "source_text", "created_by", "created_by_name", "updated_by_name", "updated_at"} <= cols
    log = {r["column_name"] for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'signal_names_log'")}
    assert {"signal_id", "plc_id", "tag", "action", "was", "now", "by_name", "at"} <= log
    p = await _plc(pool)
    with pytest.raises(Exception):
        await pool.execute("INSERT INTO signal_names (plc_id, tag, tag_key, status) VALUES ($1, 'a', 'a', 'bad')", p)


async def test_ensure_schema_is_idempotent(app, pool):
    await _sn().ensure_signal_names_schema(pool)
    await _sn().ensure_signal_names_schema(pool)


# ---------- нормализация и friendly() ----------

def test_tag_key_matches_tia_logic_norm_name():
    import tia_logic
    for t in ('ITL_GWM_DB."From GWM".BodyCheck1', "OPMode08_DB.EL300.Goto3rd", ' db1."Sens A" '):
        assert _sn().tag_key(t) == tia_logic.norm_name(t.strip())
    assert _sn().tag_key('ITL_GWM_DB."From GWM".BodyCheck1') == _sn().tag_key("itl_gwm_db.From GWM.bodycheck1")


async def test_friendly_ignores_case_and_quotes(app, users, pool):
    p = await _plc(pool)
    await _add(users["t_master"], p, 'ITL_GWM_DB."From GWM".BodyCheck1', simple_name="датчик положения кузова на выдаче", place="станция MB130")
    s = _sn()
    assert await s.friendly(pool, p, 'itl_gwm_db.From GWM.BODYCHECK1') == ("датчик положения кузова на выдаче", "станция MB130")
    assert await s.friendly(pool, p, "no.such") is None
    assert await s.friendly(pool, p + 1, 'ITL_GWM_DB."From GWM".BodyCheck1') is None   # другой ПЛК


async def test_friendly_none_for_empty_name_and_only_checked(app, users, pool):
    p = await _plc(pool)
    c = users["t_master"]
    await _add(c, p, "A.empty")
    sid = await _add(c, p, "A.draft", simple_name="кнопка", place="шкаф 3")
    s = _sn()
    assert await s.friendly(pool, p, "A.empty") is None
    assert await s.friendly(pool, p, "A.draft") == ("кнопка", "шкаф 3")
    assert await s.friendly(pool, p, "A.draft", only_checked=True) is None
    assert (await c.put(f"/api/signal-names/{sid}", json={"simple_name": "кнопка", "place": "шкаф 3", "status": "checked"})).status_code == 200
    assert await s.friendly(pool, p, "A.draft", only_checked=True) == ("кнопка", "шкаф 3")


# ---------- права ----------

async def test_anonymous_has_no_access(app, anon):
    assert (await anon.get("/api/signal-names")).status_code == 401
    assert (await anon.post("/api/signal-names", json={"plc_id": 1, "tag": "x"})).status_code == 401
    assert (await anon.get("/api/signal-names.xlsx")).status_code == 401


async def test_viewer_reads_but_cannot_change(app, users, pool):
    p = await _plc(pool)
    sid = await _add(users["t_admin"], p, "X.one", simple_name="один")
    v = users["t_viewer"]
    assert (await v.get("/api/signal-names")).status_code == 200
    assert (await v.post("/api/signal-names", json={"plc_id": p, "tag": "X.two"})).status_code == 403
    assert (await v.put(f"/api/signal-names/{sid}", json={"simple_name": "x"})).status_code == 403
    assert (await v.post("/api/signal-names/drafts", json={"plc_id": p, "output": "DB1.Motor"})).status_code == 403
    assert (await v.post("/api/signal-names/drafts-alarms", json={"plc_id": p})).status_code == 403


@pytest.mark.parametrize("login", EDITORS)
async def test_editor_roles_create_and_edit(app, users, pool, login):
    p = await _plc(pool)
    c = users[login]
    sid = await _add(c, p, "X.sig")
    r = await c.put(f"/api/signal-names/{sid}", json={"simple_name": "клапан", "place": "станция 10", "status": "draft"})
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_delete_only_author_and_admin(app, users, pool):
    p = await _plc(pool)
    sid = await _add(users["t_master"], p, "X.a")
    assert (await users["t_oto"].delete(f"/api/signal-names/{sid}")).status_code == 403
    assert (await users["t_chief"].delete(f"/api/signal-names/{sid}")).status_code == 403
    assert (await users["t_viewer"].delete(f"/api/signal-names/{sid}")).status_code == 403
    items = (await users["t_oto"].get("/api/signal-names")).json()["items"]
    assert items[0]["can_delete"] is False and (await users["t_master"].get("/api/signal-names")).json()["items"][0]["can_delete"] is True
    assert (await users["t_master"].delete(f"/api/signal-names/{sid}")).status_code == 200
    sid2 = await _add(users["t_master"], p, "X.b")
    assert (await users["t_admin"].delete(f"/api/signal-names/{sid2}")).status_code == 200
    assert (await users["t_admin"].delete(f"/api/signal-names/{sid2}")).status_code == 404


# ---------- уникальность, проверки, история ----------

async def test_unique_per_plc_ignoring_case_and_quotes(app, users, pool):
    p1, p2 = await _plc(pool, "P1"), await _plc(pool, "P2")
    c = users["t_master"]
    await _add(c, p1, 'ITL.\"From GWM\".Auto')
    r = await c.post("/api/signal-names", json={"plc_id": p1, "tag": "itl.From GWM.AUTO"})
    assert r.status_code == 409
    await _add(c, p2, 'ITL."From GWM".Auto')            # в другом ПЛК тот же сигнал допустим
    assert (await c.post("/api/signal-names", json={"plc_id": 99999, "tag": "z"})).status_code == 404


async def test_validation(app, users, pool):
    p = await _plc(pool)
    c = users["t_master"]
    assert (await c.post("/api/signal-names", json={"plc_id": p, "tag": "A", "status": "zzz"})).status_code == 422
    assert (await c.post("/api/signal-names", json={"plc_id": p, "tag": "A", "status": "checked"})).status_code == 422   # проверено без названия
    assert (await c.post("/api/signal-names", json={"plc_id": p, "tag": "A", "simple_name": "я" * 121})).status_code == 422
    assert (await c.post("/api/signal-names", json={"plc_id": p, "tag": "  "})).status_code == 422


async def test_history_keeps_who_when_was_now_and_deleted(app, users, pool):
    p = await _plc(pool)
    m, a = users["t_master"], users["t_admin"]
    sid = await _add(m, p, "H.sig", source_text="Body check")
    await m.put(f"/api/signal-names/{sid}", json={"simple_name": "датчик кузова", "place": "MB130", "status": "draft", "source_text": "Body check"})
    await m.put(f"/api/signal-names/{sid}", json={"simple_name": "датчик кузова", "place": "MB130", "status": "draft", "source_text": "Body check"})   # без изменений: записи нет
    await a.put(f"/api/signal-names/{sid}", json={"simple_name": "датчик кузова", "place": "MB130", "status": "checked", "source_text": "Body check"})
    log = (await m.get(f"/api/signal-names/{sid}/log")).json()["items"]
    assert [x["action"] for x in log] == ["create", "update", "update"]
    assert log[1]["was"]["simple_name"] == "" and log[1]["now"]["simple_name"] == "датчик кузова" and log[1]["by"].startswith("T_MASTER")
    assert log[2]["was"]["status"] == "draft" and log[2]["now"]["status"] == "checked" and log[2]["by"].startswith("T_ADMIN")
    await a.delete(f"/api/signal-names/{sid}")
    rows = await pool.fetch("SELECT action, was, now FROM signal_names_log WHERE signal_id = $1 ORDER BY id", sid)
    assert [r["action"] for r in rows] == ["create", "update", "update", "delete"] and rows[-1]["now"] is None
    assert await pool.fetchval("SELECT count(*) FROM signal_names WHERE id = $1", sid) == 0


async def test_author_is_surname_and_role(app, users, pool):
    p = await _plc(pool)
    await _add(users["t_oto"], p, "Au.sig")
    it = (await users["t_oto"].get("/api/signal-names")).json()["items"][0]
    assert it["author"] == "T_OTO, ТО"


async def test_list_filters_by_plc_status_and_search(app, users, pool):
    p1, p2 = await _plc(pool, "P1"), await _plc(pool, "P2")
    c = users["t_master"]
    await _add(c, p1, "A.one", simple_name="Лифт вверх", status="checked")
    await _add(c, p1, "A.two", source_text="Lift limit switch")
    await _add(c, p2, "B.three", place="шкаф 7")

    async def tags(q):
        return [i["tag"] for i in (await c.get("/api/signal-names", params=q)).json()["items"]]
    assert await tags({}) == ["A.one", "A.two", "B.three"]
    assert await tags({"plc_id": p1}) == ["A.one", "A.two"]
    assert await tags({"status": "checked"}) == ["A.one"]
    assert await tags({"q": "лифт"}) == ["A.one"]
    assert await tags({"q": "LIFT"}) == ["A.two"]
    assert await tags({"q": "шкаф"}) == ["B.three"]
    assert await tags({"q": "100%"}) == []
    assert (await c.get("/api/signal-names", params={"status": "zzz"})).status_code == 422


# ---------- Excel = экран ----------

def _sheet(data: bytes) -> list[list[str]]:
    z = zipfile.ZipFile(io.BytesIO(data))
    sst = [("".join(t.text or "" for t in si.iter(NS + "t"))) for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v = c.find(NS + "v")
            if v is not None:
                cells[col] = sst[int(v.text)] if c.get("t") == "s" else v.text
        rows.append([cells.get(ch, "") for ch in "ABCDEFG"])
    return rows


async def test_excel_matches_screen(app, users, pool):
    p1, p2 = await _plc(pool, "P1"), await _plc(pool, "P2")
    c = users["t_master"]
    await _add(c, p1, 'ITL."From GWM".BodyCheck1', simple_name="датчик кузова", place="MB130", status="checked", source_text="Body check 1")
    await _add(c, p2, "B.two")
    js = (STATIC / "signalnames.js").read_text(encoding="utf-8")
    screen_titles = re.findall(r"\{ k: '(?!act)\w+', t: '([^']+)'", js)
    r = await c.get("/api/signal-names.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    name = unquote(re.search(r"filename\*=UTF-8''(.+)", r.headers["content-disposition"]).group(1))
    assert name.startswith("Названия сигналов_все ПЛК_") and name.endswith(".xlsx")
    rows = _sheet(r.content)
    header = next(x for x in rows if x[0] == "ПЛК")
    assert header == screen_titles == [t for _k, t in _sn().COLUMNS]
    body = rows[rows.index(header) + 1:]
    items = (await c.get("/api/signal-names")).json()["items"]
    assert body == [[i["plc"], i["tag"], i["simple_name"], i["place"], i["status_name"], i["source_text"], i["author"]] for i in items]
    assert body[0][4] == "проверено" and body[1][4] == "черновик"
    # те же фильтры, что на экране
    r2 = await c.get("/api/signal-names.xlsx", params={"plc_id": p2, "status": "draft"})
    assert "P2" in unquote(r2.headers["content-disposition"])
    body2 = _sheet(r2.content)
    assert [x[1] for x in body2 if x[0] == "P2"] == ["B.two"] and not any(x[0] == "P1" for x in body2)


def test_screen_is_wired():
    js = (STATIC / "signalnames.js").read_text(encoding="utf-8")
    assert "attachColWidths" in js and "signalNamesSection" in js and "/api/signal-names" in js
    assert 'src="/static/signalnames.js"' in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "dict-signals" in (STATIC / "dictionary.js").read_text(encoding="utf-8")
    for t in re.findall(r'title="([^"]{3,})"', js) + re.findall(r"tip: '([^']+)'", js):   # подсказка 10–20 слов (допуск до 24 с вставками)
        if not t.startswith("${"):
            assert 10 <= len(t.split()) <= 24, t
    assert not re.search(r"font-size:\s*\d+px", js)


# ---------- заготовки ----------

@pytest.fixture
def bridge(tmp_path, monkeypatch):
    import json
    import tia
    import tia_logic
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    (tmp_path / "logic").mkdir()
    (tmp_path / "tia-projects.json").write_text(json.dumps({"projects": [{"id": "mini", "name": "Mini", "path": "x", "plc": "plc_s12"}]}), encoding="utf-8")
    return tmp_path, tia_logic.TiaLogic.load(FIX)


async def test_drafts_from_logic_leaves(app, users, pool, bridge):
    tmp, logic = bridge
    logic.to_json(tmp / "logic" / "mini.json")
    p = await _plc(pool)
    c = users["t_master"]
    r = await c.post("/api/signal-names/drafts", json={"plc_id": p, "output": "DB1.Motor"})
    assert r.status_code == 200 and r.json() == {"added": 3, "skipped": 0, "found": 3, "project": "mini"}
    items = (await c.get("/api/signal-names", params={"plc_id": p})).json()["items"]
    assert sorted(i["tag"] for i in items) == ['DB1."Sens A"', "DB1.Blocked", "DB1.Self"]
    assert all(i["status"] == "draft" and i["simple_name"] == "" and i["place"] == "" for i in items)
    # повторный запуск и ручная правка: существующие строки не трогаются
    sid = items[0]["id"]
    await c.put(f"/api/signal-names/{sid}", json={"simple_name": "моё", "place": "", "status": "draft"})
    r = await c.post("/api/signal-names/drafts", json={"plc_id": p, "output": "db1.motor"})
    assert r.json()["added"] == 0 and r.json()["skipped"] == 3
    assert [i["simple_name"] for i in (await c.get("/api/signal-names", params={"plc_id": p})).json()["items"] if i["id"] == sid] == ["моё"]


async def test_drafts_errors(app, users, pool, bridge):
    tmp, logic = bridge
    p = await _plc(pool)
    c = users["t_master"]
    assert (await c.post("/api/signal-names/drafts", json={"plc_id": p, "output": "DB1.Motor"})).status_code == 409    # файла логики нет
    logic.to_json(tmp / "logic" / "mini.json")
    assert (await c.post("/api/signal-names/drafts", json={"plc_id": p, "output": "No.Such.Out"})).status_code == 404
    other = await _plc(pool, "OTHER")
    assert (await c.post("/api/signal-names/drafts", json={"plc_id": other, "output": "DB1.Motor"})).status_code == 404   # у ПЛК нет проекта TIA
    assert (await c.post("/api/signal-names/drafts", json={"plc_id": 99999, "output": "DB1.Motor"})).status_code == 404


def test_collect_leaves_follows_intermediate_signals(bridge):
    _tmp, logic = bridge
    leaves = _sn().collect_leaves(logic, "DB1.Done")
    assert "db1.go" in leaves and "db1.delay" in leaves          # через вызов блока и таймер
    assert "db1.done" not in leaves and "db1.running" not in leaves   # сам выход и промежуточный сигнал не листья


async def test_drafts_from_alarms_top_by_count_last_7_days(app, users, pool):
    from datetime import datetime, timedelta, timezone
    p = await _plc(pool, "GWM_UB")
    now = datetime.now(timezone.utc)
    for bit, text, code, trig in [(1, "Body check timeout", "A001", "ITL_GWM_DB.BodyCheck1"), (2, "Lift limit", "A002", ""), (3, "Old alarm", "A003", "X.old")]:
        await pool.execute(
            "INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, code, text, class, trigger_tag, is_placeholder, station, device, parsed) "
            "VALUES ($1, $2, 0, $6, $3, $4, 'Alarm', $5, FALSE, '', '', TRUE)", p, bit, code, text, trig, bit)
    for bit, n, ago in [(1, 5, 1), (2, 2, 2), (3, 9, 30)]:
        for k in range(n):
            await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at) VALUES ($1, $2, $3, $4)",
                               p, bit, now - timedelta(days=ago, minutes=k * 10), now - timedelta(days=ago, minutes=k * 10 - 1))
    c = users["t_oto"]
    r = await c.post("/api/signal-names/drafts-alarms", json={"plc_id": p, "limit": 20, "days": 7})
    assert r.status_code == 200 and r.json() == {"added": 2, "skipped": 0, "found": 2}
    items = (await c.get("/api/signal-names", params={"plc_id": p})).json()["items"]
    assert {i["tag"]: i["source_text"] for i in items} == {"ITL_GWM_DB.BodyCheck1": "A001 Body check timeout", "alarm_bit_2": "A002 Lift limit"}
    assert {i["status"] for i in items} == {"draft"} and all(i["simple_name"] == "" for i in items)
    assert "X.old" not in [i["tag"] for i in items]                  # старше 7 дней не берётся
    top1 = (await c.post("/api/signal-names/drafts-alarms", json={"plc_id": p, "limit": 1, "days": 7})).json()
    assert top1["found"] == 1 and top1["added"] == 0                 # самый частый уже есть
