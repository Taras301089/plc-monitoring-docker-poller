"""Время ПЛК и расхождение с сервером (задача 27): расчёт, форматирование «+5м 28с», API, тайм-аут, вид в списке ПЛК."""
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
sys.path.insert(0, str(ROOT / "fastapi-api"))
sys.path.append(str(ROOT / "python-async-poller"))   # в конец: у поллера свой main.py

import plctime as pt  # noqa: E402

JS = STATIC / "plctime.js"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
REF = (STATIC / "reference.html").read_text(encoding="utf-8")
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")

# секунды расхождения -> текст (одни и те же ожидания для сервера и для JS)
CASES = {
    328: "+5м 28с", -107: "-1м 47с", -60: "-1м 0с", 24: "+24с", -1: "-1с", 1: "+1с", 0: "0с",
    20: "+20с", 11: "+11с", 3600: "+60м 0с", -3661: "-61м 1с",
}


def run(expr: str):
    code = f"const r = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


# ---------- расчёт расхождения ----------

def test_drift_sign_plc_ahead_is_plus():
    assert pt.compute_drift(1000.0 + 328, 1000.0, 1000.0) == 328
    assert pt.compute_drift(1000.0 - 107, 1000.0, 1000.0) == -107


def test_drift_uses_middle_of_request():
    # запрос шёл 4 с: сервер «в момент ответа ПЛК» = середина (102), ПЛК показал 107 -> +5
    assert pt.compute_drift(107.0, 100.0, 104.0) == 5
    assert pt.compute_drift(102.0, 100.0, 104.0) == 0


def test_drift_rounding_to_whole_seconds():
    assert pt.compute_drift(100.4, 100.0, 100.0) == 0
    assert pt.compute_drift(100.6, 100.0, 100.0) == 1
    assert pt.compute_drift(99.6, 100.0, 100.0) == 0
    assert pt.compute_drift(98.6, 100.0, 100.0) == -1
    assert pt.compute_drift(100.5, 100.0, 100.0) == 1


def test_parse_plc_time_iso_with_and_without_zone():
    a = pt.parse_plc_time("2026-10-09T11:22:33.500000+00:00")
    b = pt.parse_plc_time("2026-10-09T11:22:33.500000")        # без зоны: OPC UA отдаёт UTC
    assert a == b == datetime(2026, 10, 9, 11, 22, 33, 500000, tzinfo=timezone.utc).timestamp()
    assert pt.parse_plc_time("2026-10-09T16:22:33+05:00") == datetime(2026, 10, 9, 11, 22, 33, tzinfo=timezone.utc).timestamp()
    assert pt.parse_plc_time(None) is None and pt.parse_plc_time("мусор") is None and pt.parse_plc_time(5) is None


def test_poller_serializes_datetime_as_iso():
    from opc_client import _to_jsonable
    assert pt.parse_plc_time(_to_jsonable(datetime(2026, 10, 9, 11, 22, 33))) is not None
    assert pt.NODE_ID == "i=2258"                              # стандартный узел «текущее время сервера» OPC UA
    from asyncua import ua
    n = ua.NodeId.from_string(pt.NODE_ID)
    assert n.Identifier == 2258 and n.NamespaceIndex == 0      # poller разбирает так же (read_values)


# ---------- форматирование ----------

def test_fmt_drift_server():
    for sec, text in CASES.items():
        assert pt.fmt_drift(sec) == text, sec
    assert pt.fmt_drift(None) == "—"


@node
def test_fmt_drift_js_matches_server():
    for sec, text in CASES.items():
        assert run(f"r.fmtDrift({sec})") == text == pt.fmt_drift(sec), sec
    assert run("r.fmtDrift(null)") == "—" and run("r.fmtDrift(undefined)") == "—"


def test_red_threshold_more_than_5_seconds():
    assert pt.DRIFT_LIMIT_SEC == 5
    assert [pt.is_drifted(x) for x in (0, 5, -5, 6, -6, 328)] == [False, False, False, True, True, True]


@node
def test_red_threshold_js():
    assert [run(f"r.isDrifted({x})") for x in (0, 5, -5, 6, -6, 328)] == [False, False, False, True, True, True]
    assert run("r.isDrifted(null)") is False


@node
def test_fragment_html_js():
    ok = run("r.fragment({ok:true, plc_time:'2026-10-09T07:41:05+00:00', drift_sec:328, read_at:'2026-10-09T07:41:05+00:00'}, "
             "ms => 'T' + new Date(ms).getUTCHours(), () => Date.parse('2026-10-09T07:41:05+00:00'))")
    assert "T7 · +5м 28с" in ok and "plc-time bad" in ok and "Время ПЛК и расхождение с сервером PMM" in ok
    near = run("r.fragment({ok:true, plc_time:'2026-10-09T07:41:05+00:00', drift_sec:3, read_at:'2026-10-09T07:41:05+00:00'}, "
               "() => '12:41:05', () => Date.parse('2026-10-09T07:41:05+00:00'))")
    assert "12:41:05 · +3с" in near and "bad" not in near
    zero = run("r.fragment({ok:true, plc_time:'2026-10-09T07:41:05+00:00', drift_sec:0, read_at:'2026-10-09T07:41:05+00:00'}, () => '12:41:05', () => 0)")
    assert "12:41:05 · 0с" in zero
    for bad in ("null", "{ok:false}", "{ok:true, plc_time:null, drift_sec:null}"):
        out = run(f"r.fragment({bad}, () => 'x', () => 0)")
        assert ">—<" in out and "bad" not in out


@node
def test_fragment_time_advances_since_read():
    # время ПЛК, показанное через 30 с после чтения, ушло вперёд на 30 с
    out = run("r.fragment({ok:true, plc_time:'2026-10-09T07:41:05+00:00', drift_sec:1, read_at:'2026-10-09T07:41:05+00:00'}, "
              "ms => String(ms), () => Date.parse('2026-10-09T07:41:35+00:00'))")
    assert str(int(datetime(2026, 10, 9, 7, 41, 35, tzinfo=timezone.utc).timestamp() * 1000)) in out


# ---------- чтение и кэш (подставной клиент ПЛК) ----------

@pytest.fixture(autouse=True)
def _clean_cache():
    pt.CACHE.clear()
    yield
    pt.CACHE.clear()


def _iso(plus: float) -> str:
    return datetime.fromtimestamp(time.time() + plus, tz=timezone.utc).isoformat()


async def test_read_one_ok_and_node_list(monkeypatch):
    seen = []

    async def reader(plc_id, node_ids):
        seen.append((plc_id, node_ids))
        return {pt.NODE_ID: {"ok": True, "v": _iso(328), "t": "DateTime", "s": ""}}

    monkeypatch.setattr(pt, "read_values", reader)
    r = await pt.read_one(7)
    assert seen == [(7, ["i=2258"])]
    assert r["plc_id"] == 7 and r["ok"] is True and abs(r["drift_sec"] - 328) <= 1
    assert r["plc_time"].endswith("+00:00") or r["plc_time"].endswith("Z")
    assert r["read_at"]


@pytest.mark.parametrize("answer", [
    {pt.NODE_ID: {"ok": False, "v": None, "t": "", "s": "BadNodeIdUnknown"}},
    {pt.NODE_ID: {"ok": True, "v": "не дата", "t": "String", "s": ""}},
    {},
])
async def test_read_one_bad_value_gives_not_ok(monkeypatch, answer):
    async def reader(plc_id, node_ids):
        return answer

    monkeypatch.setattr(pt, "read_values", reader)
    r = await pt.read_one(1)
    assert r["ok"] is False and r["plc_time"] is None and r["drift_sec"] is None


async def test_read_one_no_link_gives_not_ok(monkeypatch):
    async def reader(plc_id, node_ids):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(pt, "read_values", reader)
    r = await pt.read_one(1)
    assert r["ok"] is False and r["drift_sec"] is None and r["read_at"]


async def test_hung_plc_does_not_block_others(monkeypatch):
    monkeypatch.setattr(pt, "READ_TIMEOUT_SEC", 0.3)

    async def reader(plc_id, node_ids):
        if plc_id == 2:
            await asyncio.sleep(60)
        return {pt.NODE_ID: {"ok": True, "v": _iso(10), "t": "DateTime", "s": ""}}

    monkeypatch.setattr(pt, "read_values", reader)
    t0 = time.monotonic()
    res = await pt.read_many([1, 2, 3])
    assert time.monotonic() - t0 < 2                     # параллельно: ждали один тайм-аут, а не сумму
    by = {r["plc_id"]: r for r in res}
    assert by[1]["ok"] and by[3]["ok"] and not by[2]["ok"]
    assert [r["plc_id"] for r in res] == [1, 2, 3]       # порядок как запрошен


async def test_timeout_is_at_most_5_seconds():
    assert 0 < pt.READ_TIMEOUT_SEC <= 5


async def test_fake_client_without_write(monkeypatch):
    """Чтение времени не вызывает запись в ПЛК: подставной клиент с write_int, который падает при вызове."""
    class Fake:
        def __init__(self):
            self.writes = 0

        async def write_int(self, *a, **k):
            self.writes += 1
            raise AssertionError("запись в ПЛК")

        async def read_values(self, node_ids):
            return {n: {"ok": True, "v": _iso(1), "t": "DateTime", "s": ""} for n in node_ids}

    fake = Fake()

    async def reader(plc_id, node_ids):
        return await fake.read_values(node_ids)

    monkeypatch.setattr(pt, "read_values", reader)
    await pt.read_many([1, 2])
    assert fake.writes == 0


def test_module_has_no_write_path():
    src = (ROOT / "fastapi-api" / "plctime.py").read_text(encoding="utf-8").lower()
    assert "write" not in src and "plan-day" not in src
    js = JS.read_text(encoding="utf-8").lower()
    assert "write" not in js and "fetch(" not in js        # скрипт только показывает, запросы делает index.html


# ---------- API ----------

async def _plc(pool, name, active=True):
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.0.0.1:4840', $2) RETURNING id", name, active)


async def test_api_time_ok_and_not_ok(app, anon, pool, monkeypatch):
    a = await _plc(pool, "t_a")
    b = await _plc(pool, "t_b")
    off = await _plc(pool, "t_off", active=False)
    calls = []

    async def reader(plc_id, node_ids):
        calls.append(plc_id)
        if plc_id == b:
            raise ConnectionError("нет связи")
        return {pt.NODE_ID: {"ok": True, "v": _iso(-107), "t": "DateTime", "s": ""}}

    monkeypatch.setattr(pt, "read_values", reader)
    r = await anon.get("/api/plcs/time")                  # вход не нужен
    assert r.status_code == 200
    by = {x["plc_id"]: x for x in r.json()}
    assert set(by) == {a, b, off}
    assert by[a]["ok"] is True and abs(by[a]["drift_sec"] + 107) <= 1 and by[a]["plc_time"] and by[a]["read_at"]
    assert by[b]["ok"] is False and by[b]["plc_time"] is None and by[b]["drift_sec"] is None
    assert by[off]["ok"] is False and off not in calls     # выключенный из опроса ПЛК не читается
    # повторный запрос в пределах минуты берёт кэш и не нагружает ПЛК
    n = len(calls)
    await anon.get("/api/plcs/time")
    assert len(calls) == n


async def test_api_time_rereads_after_cache_expired(app, anon, pool, monkeypatch):
    a = await _plc(pool, "t_c")
    calls = []

    async def reader(plc_id, node_ids):
        calls.append(plc_id)
        return {pt.NODE_ID: {"ok": True, "v": _iso(0), "t": "DateTime", "s": ""}}

    monkeypatch.setattr(pt, "read_values", reader)
    await anon.get("/api/plcs/time")
    pt.CACHE[a]["mono"] -= pt.CACHE_TTL_SEC + 1
    await anon.get("/api/plcs/time")
    assert calls.count(a) == 2


async def test_api_time_after_failure_gives_no_old_numbers(app, anon, pool, monkeypatch):
    a = await _plc(pool, "t_d")
    state = {"fail": False}

    async def reader(plc_id, node_ids):
        if state["fail"]:
            raise ConnectionError("нет связи")
        return {pt.NODE_ID: {"ok": True, "v": _iso(5), "t": "DateTime", "s": ""}}

    monkeypatch.setattr(pt, "read_values", reader)
    assert (await anon.get("/api/plcs/time")).json()[0]["ok"] is True
    state["fail"] = True
    pt.CACHE[a]["mono"] -= pt.CACHE_TTL_SEC + 1
    x = (await anon.get("/api/plcs/time")).json()[0]
    assert x["ok"] is False and x["drift_sec"] is None and x["plc_time"] is None


# ---------- интерфейс ----------

def test_script_connected_after_plant_time():
    assert '<script src="/static/plctime.js"></script>' in HTML
    assert HTML.index("/static/plant-time.js") < HTML.index("/static/plctime.js") < HTML.index("/static/reorder.js")


def test_list_shows_fragment_with_timeout_and_minute_refresh():
    assert "/api/plcs/time" in HTML and "AbortSignal.timeout(6000)" in HTML[HTML.index("/api/plcs/time"):HTML.index("/api/plcs/time") + 400]
    assert "setInterval(refreshPlcTime, 60000)" in HTML
    assert "plcTime.fragment(" in HTML
    # и в строках списка, и в закрытом поле
    assert HTML.count("plcTimeHtml(") >= 3
    assert "plantClock" in JS.read_text(encoding="utf-8") or "plantClock" in HTML[HTML.index("function plcTimeHtml"):HTML.index("function plcTimeHtml") + 600]


def test_fragment_title_and_colors():
    js = JS.read_text(encoding="utf-8")
    assert ("Время ПЛК и расхождение с сервером PMM (плюс — ПЛК впереди). Красным, если расхождение больше 5 секунд. "
            "Синхронизация времени настраивается в TIA Portal (NTP) в каждом процессоре") in js
    css = "\n".join(l for l in HTML.splitlines() if ".plc-time" in l or ".plc-dd-time" in l)
    assert css and "position" not in css and "#" not in css
    assert "var(--danger)" in css and "var(--fg-tertiary)" in css


def test_row_wraps_instead_of_overlapping():
    row = next(l for l in HTML.splitlines() if l.strip().startswith(".plc-dd-row {"))
    assert "flex-wrap: wrap" in row and " height: 34px" not in row and "min-height: 34px" in row


def test_no_duplicate_words_in_fragment():
    js = JS.read_text(encoding="utf-8")
    shown = [l for l in js.splitlines() if "<span" in l]
    assert shown and all("расхожд" not in l.split('title="')[0].lower() and "время" not in l.split('title="')[0].lower() for l in shown)


# ---------- справка ----------

def test_reference_has_ntp_section():
    assert "Синхронизация времени ПЛК (NTP)" in REF
    sec = REF[REF.index("Синхронизация времени ПЛК (NTP)"):]
    for word in ("TIA Portal", "Time of day", "NTP", "UTC+5", "Astana", "60 с", "PMM время ПЛК не меняет", "plctime.py", "plctime.js"):
        assert word in REF, word
    assert "Enable time-of-day synchronization via NTP server" in sec
