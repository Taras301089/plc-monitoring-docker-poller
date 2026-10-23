"""Запись расписания интервалов в ПЛК: проверка значений и защищённая запись массива (только prod)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "python-async-poller"))

from intervals import validate_schedule  # noqa: E402
from models import Plc  # noqa: E402
from opc_client import PlcOpcClient  # noqa: E402

START = [450, 510, 580, 630, 710, 780, 840, 910, 960, 1020, 1080, 1150, 1200]
END = [510, 570, 630, 670, 780, 840, 900, 960, 990, 1080, 1140, 1200, 1230]


def test_current_schedule_is_valid():
    assert validate_schedule(START, END) is None


def test_needs_13_elements():
    assert "13" in validate_schedule(START[:12], END[:12])
    assert "13" in validate_schedule(START + [1300], END + [1310])


def test_start_must_be_less_than_end():
    end = list(END)
    end[4] = START[4]
    assert "начало" in validate_schedule(START, end).lower()


def test_range_is_0_to_1440():
    assert validate_schedule([-1] + START[1:], END) is not None
    assert validate_schedule(START, END[:-1] + [1441]) is not None


def test_end_not_after_next_start():
    end = list(END)
    end[0] = START[1] + 1                  # конец периода 1 заходит на начало периода 2
    assert validate_schedule(START, end) is not None


def test_starts_must_ascend():
    start = list(START)
    start[3], start[4] = start[4], start[3]
    assert validate_schedule(start, END) is not None


def test_non_integers_rejected():
    assert validate_schedule(["450"] + START[1:], END) is not None
    assert validate_schedule([True] + START[1:], END) is not None


async def test_write_dint_array_is_blocked_in_dev(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    client = PlcOpcClient(Plc(id=1, name="T", opc_endpoint="opc.tcp://10.0.0.1:4840", poll_interval_ms=1000))
    called = []

    async def connected():
        called.append(1)
        return True

    client._ensure_connected_unlocked = connected
    with pytest.raises(PermissionError, match="Режим разработки"):
        await client.write_dint_array("ns=3;s=Arr", START)
    assert called == []                     # до подключения к ПЛК дело не дошло


async def test_write_dint_array_reaches_plc_layer_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    client = PlcOpcClient(Plc(id=1, name="T", opc_endpoint="opc.tcp://10.0.0.1:4840", poll_interval_ms=1000))

    async def not_connected():
        return False

    client._ensure_connected_unlocked = not_connected
    with pytest.raises(ConnectionError):
        await client.write_dint_array("ns=3;s=Arr", START)


# ---------- API: запись границ интервалов из окна «Смена и план» ----------

START13 = START
END13 = END


async def _brand_screen(pool):
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_sw', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    return await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('Chery T', $1, 'body_counter', $2::jsonb) RETURNING id",
        plc, '{"sched_start": "ns=3;s=S", "sched_end": "ns=3;s=E"}')


async def test_schedule_plc_blocked_in_dev(app, pool, users, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    scr = await _brand_screen(pool)
    r = await users["t_chief"].post(f"/api/kpi/screens/{scr}/schedule-plc", json={"start": START13, "end": END13})
    assert r.status_code == 403 and "Режим разработки" in r.json()["detail"]


async def test_schedule_plc_roles(app, pool, users, monkeypatch):
    import main
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setattr(main, "POLLER_URL", "http://127.0.0.1:9")
    scr = await _brand_screen(pool)
    r = await users["t_viewer"].post(f"/api/kpi/screens/{scr}/schedule-plc", json={"start": START13, "end": END13})
    assert r.status_code == 403                        # роль «Просмотр» не пишет
    for login in ("t_admin", "t_chief", "t_area", "t_master", "t_oto"):   # остальные роли доходят до поллера (его адрес в тесте недоступен)
        r = await users[login].post(f"/api/kpi/screens/{scr}/schedule-plc", json={"start": START13, "end": END13})
        assert r.status_code == 502 and "Поллер недоступен" in r.json()["detail"], login


async def test_schedule_plc_needs_bindings(app, pool, users, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('t_sw2', 'opc.tcp://127.0.0.1:4840') RETURNING id")
    scr = await pool.fetchval(
        "INSERT INTO kpi_screens (name, plc_id, template, bindings) VALUES ('NoSched', $1, 'body_counter', '{}'::jsonb) RETURNING id", plc)
    r = await users["t_chief"].post(f"/api/kpi/screens/{scr}/schedule-plc", json={"start": START13, "end": END13})
    assert r.status_code == 422


# ---------- окно «Смена и план»: редактируемое начало ----------

def _dialog():
    html = (ROOT / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("function openBrandPlanDialog(screen) {")
    return html[i: html.index("function paintKpi(screen, v) {", i)]


def test_window_start_is_editable_with_hint_and_role_limit():
    d = _dialog()
    assert 'data-start="${i}"' in d and 'type="time"' in d[d.index('data-start="${i}"') - 60: d.index('data-start="${i}"')]
    assert "const canPlc = !!(screen.bindings && screen.bindings.sched_start" in d     # право проверяется и на сервере; поле блокируется без привязки
    assert "${canPlc ? '' : ' disabled'}" in d
    i = d.index('data-start="${i}"')
    assert len(d[i: i + 600].split('title="')[1].split('"')[0].split()) >= 10      # у поля подсказка 10-20 слов


def test_window_writes_plc_before_site_and_stops_on_error():
    d = _dialog()
    a, b = d.index("/schedule-plc"), d.index("/schedule`")
    assert a < b                                                       # границы сначала в ПЛК: при отказе на сайте ничего не меняется
    assert "Режим разработки" in d                                      # в разработке окончания сохраняются только на сайте
    assert "dp.applied" in d                                            # сообщение, применил ли ПЛК новые границы
