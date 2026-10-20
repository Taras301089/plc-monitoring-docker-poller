"""Аварии самого ПЛК по OPC UA Alarms & Conditions: разбор событий, запись в alarm_ac_events, флажок plcs.ac_alarms."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "python-async-poller"))

from asyncua import ua  # noqa: E402
from asyncua.common.events import Event  # noqa: E402

import ac_collector  # noqa: E402
from ac_collector import AcCollector, PlcAcState, close_missing, event_fields, parse_event  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
EP = "opc.tcp://10.0.0.1:4840"
T0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)
COND = 'ns=3;s="PLC_1"."Diag"."Alarm_1"'


def _ev(active, acked=None, cond=COND, t=T0, **extra):
    f = {
        "EventType": ua.NodeId(ua.ObjectIds.OffNormalAlarmType),
        "NodeId": ua.NodeId.from_string(cond),
        "SourceName": "PN-IO",
        "SourceNode": ua.NodeId(5, 3),
        "ConditionName": "Модуль",
        "Message": ua.LocalizedText("Ошибка модуля ET200"),
        "Severity": 700,
        "Time": t,
        "ActiveState/Id": active,
        "ActiveState/TransitionTime": t,
    }
    if acked is not None:
        f["AckedState/Id"] = acked
    f.update(extra)
    return f


REFRESH_START = {"EventType": ua.NodeId(2787), "SourceName": "Server", "Severity": 0}
REFRESH_END = {"EventType": ua.NodeId(2788), "SourceName": "Server", "Severity": 0}


# ---------- разбор событий ----------
def test_active_event_opens_with_fields():
    (a,) = parse_event(_ev(True))
    assert a.kind == "open" and a.condition_id == COND and a.source == "PN-IO"
    assert a.message == "Ошибка модуля ET200" and a.severity == 700 and a.condition_name == "Модуль" and a.ts == T0


def test_inactive_event_closes_and_acked_adds_ack():
    assert [a.kind for a in parse_event(_ev(False))] == ["close"]
    assert [a.kind for a in parse_event(_ev(False, acked=True))] == ["close", "ack"]
    assert [a.kind for a in parse_event(_ev(True, acked=False))] == ["open"]


def test_refresh_markers_and_ignore():
    assert [a.kind for a in parse_event(REFRESH_START)] == ["refresh_start"]
    assert [a.kind for a in parse_event(REFRESH_END)] == ["refresh_end"]
    assert [a.kind for a in parse_event({"EventType": ua.NodeId(2041), "Message": ua.LocalizedText("x")})] == ["ignore"]


def test_condition_id_fallback_and_naive_time_is_utc():
    f = _ev(True, t=datetime(2026, 10, 15, 8, 0, 0))
    del f["NodeId"]
    (a,) = parse_event(f)
    assert a.condition_id == "ns=3;i=5|Модуль" and a.ts == T0


def test_event_fields_from_asyncua_event():
    def clause(*path, attr=ua.AttributeIds.Value):
        op = ua.SimpleAttributeOperand()
        op.AttributeId = attr
        op.BrowsePath = [ua.QualifiedName(p, 0) for p in path]
        return op
    clauses = [clause("EventType"), clause("ActiveState", "Id"), clause("Message"), clause(attr=ua.AttributeIds.NodeId)]
    values = [ua.Variant(ua.NodeId(9341)), ua.Variant(True), ua.Variant(ua.LocalizedText("m")),
              ua.Variant(ua.NodeId.from_string(COND))]
    f = event_fields(Event.from_event_fields(clauses, values))
    assert f["ActiveState/Id"] is True and f["NodeId"] == ua.NodeId.from_string(COND)
    assert parse_event(f)[0].kind == "open" and parse_event(f)[0].condition_id == COND


def test_collector_never_writes_or_acknowledges():
    """В коде (не в комментариях) нет записи в ПЛК; единственный вызов метода: ConditionRefresh."""
    import ast
    tree = ast.parse((ROOT / "python-async-poller" / "ac_collector.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {n for n in names if n.startswith(("write", "set_value")) or n in ("Acknowledge", "Confirm")}, names
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "call_method"]
    assert len(calls) == 1 and "ConditionType_ConditionRefresh" in ast.unparse(calls[0].args[0])


# ---------- запись в базу ----------
async def _plc(pool, name="ac1", ac=False, active=True):
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, ac_alarms, is_active) VALUES ($1, $2, $3, $4) RETURNING id", name, EP, ac, active)


async def _rows(pool, plc):
    return [dict(r) for r in await pool.fetch(
        "SELECT condition_id, source, message, severity, started_at, ended_at, acked_at, active_at_start "
        "FROM alarm_ac_events WHERE plc_id = $1 ORDER BY id", plc)]


async def test_schema_is_idempotent(app, pool):
    from alarms import ensure_alarms_schema
    import main
    await ensure_alarms_schema(pool)
    await main.ensure_plc_order(pool)
    cols = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'alarm_ac_events'")}
    assert {"condition_id", "source", "condition_name", "message", "severity", "started_at", "ended_at",
            "acked_at", "received_at", "active_at_start", "plc_id"} <= cols
    assert await pool.fetchval(
        "SELECT column_default FROM information_schema.columns WHERE table_name = 'plcs' AND column_name = 'ac_alarms'") == "false"


async def test_open_close_ack_and_no_duplicate_open(app, pool):
    plc = await _plc(pool)
    st = PlcAcState(lambda: pool, plc)
    await st.handle(_ev(True), T0)
    await st.handle(_ev(True), T0 + timedelta(seconds=1))          # повтор не создаёт вторую открытую строку
    rows = await _rows(pool, plc)
    assert len(rows) == 1 and rows[0]["ended_at"] is None and rows[0]["active_at_start"] is False
    assert rows[0]["message"] == "Ошибка модуля ET200" and rows[0]["severity"] == 700 and rows[0]["source"] == "PN-IO"
    t1 = T0 + timedelta(seconds=40)
    await st.handle(_ev(False, acked=True, t=t1, **{"AckedState/TransitionTime": t1}), t1)
    rows = await _rows(pool, plc)
    assert rows[0]["ended_at"] == t1 and rows[0]["acked_at"] == t1
    await st.handle(_ev(True, t=t1 + timedelta(minutes=1)))         # новое появление: новая строка
    assert len(await _rows(pool, plc)) == 2


async def test_ack_while_active(app, pool):
    plc = await _plc(pool)
    st = PlcAcState(lambda: pool, plc)
    await st.handle(_ev(True, acked=False), T0)
    await st.handle(_ev(True, acked=True, t=T0 + timedelta(seconds=5)), T0 + timedelta(seconds=5))
    (row,) = await _rows(pool, plc)
    assert row["ended_at"] is None and row["acked_at"] == T0 + timedelta(seconds=5)


async def test_refresh_opens_current_with_flag_and_closes_missing(app, pool):
    plc = await _plc(pool)
    other = await _plc(pool, "ac2")
    st = PlcAcState(lambda: pool, plc)
    await st.handle(_ev(True, cond="ns=3;s=gone"), T0)              # было открыто до обрыва связи
    await st.handle(_ev(True, cond="ns=3;s=still"), T0)
    await PlcAcState(lambda: pool, other).handle(_ev(True, cond="ns=3;s=gone"), T0)   # чужой ПЛК не трогается
    now = T0 + timedelta(minutes=10)
    for f in (REFRESH_START, _ev(True, cond="ns=3;s=still"), _ev(True, cond="ns=3;s=new"), REFRESH_END):
        await st.handle(f, now)
    rows = {r["condition_id"]: r for r in await _rows(pool, plc)}
    assert rows["ns=3;s=gone"]["ended_at"] == now                    # пропавшее закрыто временем получения RefreshEnd
    assert rows["ns=3;s=still"]["ended_at"] is None and rows["ns=3;s=still"]["active_at_start"] is False
    assert rows["ns=3;s=new"]["ended_at"] is None and rows["ns=3;s=new"]["active_at_start"] is True
    assert (await _rows(pool, other))[0]["ended_at"] is None
    assert st.in_refresh is False


async def test_refresh_end_without_start_closes_nothing(app, pool):
    plc = await _plc(pool)
    st = PlcAcState(lambda: pool, plc)
    await st.handle(_ev(True), T0)
    await st.handle(REFRESH_END, T0)
    assert (await _rows(pool, plc))[0]["ended_at"] is None
    assert await close_missing(pool, plc, {COND}, T0) == 0


async def test_collector_follows_flag_without_restart(app, pool, monkeypatch):
    on = await _plc(pool, "on", ac=True)
    await _plc(pool, "off", ac=False)
    await _plc(pool, "disabled", ac=True, active=False)
    started = []

    async def fake_loop(self, plc_id, cfg):
        started.append(plc_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(AcCollector, "_plc_loop", fake_loop)
    col = AcCollector(lambda: pool, asyncio.Event())
    await col._reconcile()
    await asyncio.sleep(0)
    assert set(col._tasks) == {on} and started == [on]
    await pool.execute("UPDATE plcs SET ac_alarms = FALSE WHERE id = $1", on)
    await col._reconcile()
    assert col._tasks == {}


# ---------- флажок в API и окне ПЛК ----------
async def _flags(client, plc_id):
    return {p["id"]: p["ac_alarms"] for p in (await client.get("/api/plcs")).json()}[plc_id]


async def test_api_flag_default_off_saved_and_kept(users):
    adm = users["t_admin"]
    r = await adm.post("/api/plcs", json={"name": "A", "opc_endpoint": EP})
    pid = r.json()["id"]
    assert r.json()["ac_alarms"] is False and await _flags(adm, pid) is False
    r = await adm.put(f"/api/plcs/{pid}", json={"name": "A", "opc_endpoint": EP, "ac_alarms": True})
    assert r.status_code == 200 and r.json()["ac_alarms"] is True and await _flags(adm, pid) is True
    r = await adm.put(f"/api/plcs/{pid}", json={"name": "B", "opc_endpoint": EP})   # без поля: значение остаётся
    assert r.status_code == 200 and await _flags(adm, pid) is True
    r = await adm.post("/api/plcs", json={"name": "C", "opc_endpoint": EP, "ac_alarms": True})
    assert await _flags(adm, r.json()["id"]) is True


async def test_only_admin_changes_flag(users, anon):
    pid = (await users["t_admin"].post("/api/plcs", json={"name": "A", "opc_endpoint": EP})).json()["id"]
    body = {"name": "A", "opc_endpoint": EP, "ac_alarms": True}
    assert (await anon.put(f"/api/plcs/{pid}", json=body)).status_code == 401
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        assert (await users[login].put(f"/api/plcs/{pid}", json=body)).status_code == 403, login
    assert await _flags(users["t_admin"], pid) is False


def test_forms_have_ac_checkbox_with_title_and_send_flag():
    for cid in ("new-plc-ac", "edit-plc-ac"):
        i = HTML.index(f'id="{cid}"')
        tag = HTML[HTML.rfind("<input", 0, i): HTML.index(">", i)]
        assert 'type="checkbox"' in tag and "title=" in tag and "Alarms &amp; Conditions" in tag, cid
        assert HTML[HTML.index(">", i) + 1:].lstrip().startswith("Аварии ПЛК (OPC UA)"), cid
    assert "ac_alarms: document.getElementById('edit-plc-ac').checked" in HTML
    assert "ac_alarms: document.getElementById('new-plc-ac').checked" in HTML
    assert "document.getElementById('edit-plc-ac').checked = item.ac_alarms === true" in HTML


def test_poller_starts_ac_collector():
    src = (ROOT / "python-async-poller" / "main.py").read_text(encoding="utf-8")
    assert "from ac_collector import AcCollector" in src and "AcCollector(db.get_pool, service._stop).run()" in src
    assert ac_collector._PUBLISH_MS >= 200
