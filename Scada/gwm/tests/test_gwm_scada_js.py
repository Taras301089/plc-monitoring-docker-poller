"""Поведение GWM: состояния, направление, отсутствие данных и причины Error."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")


def run(expr):
    code = f"const c=require({json.dumps(str(STATIC / 'gwm-scada.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(
        ["node", "-e", code], capture_output=True, text=True, encoding="utf-8",
        timeout=30, check=True,
    ).stdout
    return json.loads(out)


@needs_node
@pytest.mark.parametrize("code,style,label", [
    (1, "error", "Error"),
    (2, "manual", "Manual Mode"),
    (3, "auto", "Auto Mode"),
    (4, "undefined", "Не уточнено"),
])
def test_plc_color_codes_show_the_four_requested_modes(code, style, label):
    for value in [code, str(code)]:
        state = run(f"c.mode({json.dumps(value)})")
        assert state["className"] == style
        assert state["label"] == label


@needs_node
def test_missing_color_is_distinct_from_an_unrecognized_plc_code():
    states = run("[null, undefined, '', '   ', false, 0, 99].map(c.mode)")
    assert all(state["className"] == "missing" for state in states)
    assert all(state["label"] == "Нет данных" for state in states[:5])
    assert states[5]["label"] == "Неизвестный код 0"
    assert states[6]["label"] == "Неизвестный код 99"


@needs_node
def test_boolean_strings_do_not_turn_false_or_missing_signals_on():
    assert run("[true, 1, '1', 'true', 'TRUE'].map(c.bool)") == [True] * 5
    assert run("[false, 0, '0', 'false', 'FALSE'].map(c.bool)") == [False] * 5
    assert run("[null, undefined, '', 'unknown', 2].map(c.bool)") == [None] * 5
    station = run("""c.normalize({
        color: '3', RUN_FWD: 'false', RUN_BWD: '0',
        sensors: {OutLet: 'FALSE', End: 'TRUE', Slow: null, Start: 0}
    })""")
    assert station["RUN_FWD"] is False and station["RUN_BWD"] is False
    assert station["sensors"] == {
        "OutLet": False, "End": True, "Slow": None, "Start": False, "InLet": None,
    }
    missing = run("c.normalize(null)")
    assert missing["color"] is None
    assert missing["RUN_FWD"] is None and missing["RUN_BWD"] is None
    assert all(value is None for value in missing["sensors"].values())


@needs_node
@pytest.mark.parametrize("fwd,bwd,reverse,key,arrow", [
    (True, False, False, "forward", "→"),
    (False, True, False, "backward", "←"),
    (True, False, True, "forward", "←"),
    (False, True, True, "backward", "→"),
])
def test_run_signals_follow_top_and_return_row_directions(fwd, bwd, reverse, key, arrow):
    state = run(f"c.motion({json.dumps(fwd)}, {json.dumps(bwd)}, {json.dumps(reverse)})")
    assert state["key"] == key
    assert state["arrow"] == arrow


@needs_node
def test_stopped_conflicting_and_missing_signals_have_no_single_direction():
    stopped, conflict, *missing = run("""[
        c.motion('false', '0', false), c.motion(true, true, true),
        c.motion(null, false, false), c.motion(true, null, false),
        c.motion(false, undefined, true), c.motion(undefined, true, true)
    ]""")
    assert stopped["key"] == "stopped" and stopped["arrow"] == "−"
    assert conflict["key"] == "conflict" and conflict["arrow"] == "↔"
    assert all(state["key"] == "unknown" and state["arrow"] == "—" for state in missing)


@needs_node
def test_failed_or_partial_snapshot_never_keeps_old_carid_or_green_state():
    data = run("c.normalizeSnapshot({MB200: {color: 3, carid: 'VIN123', carid_status: 'ok'}})")
    assert data["MB200"]["carid"] == "VIN123"
    assert data["UB10"]["color"] is None
    assert data["UB10"]["carid"] is None
    failed = run("c.normalizeSnapshot(null)")
    assert all(s["color"] is None and s["carid"] is None for s in failed.values())
    assert all(s["RUN_FWD"] is None and s["RUN_BWD"] is None for s in failed.values())


@needs_node
def test_layout_contains_every_station_once():
    station_ids = run("c.STATIONS")
    expected = {f"UB{number}" for number in range(10, 100, 10)}
    expected.update(f"MB{number}" for number in range(100, 280, 10))
    assert len(station_ids) == len(set(station_ids)) == 27
    assert set(station_ids) == expected
    assert set(run("Object.keys(c.normalizeSnapshot(null))")) == expected


@needs_node
def test_error_without_confirmed_cause_is_not_claimed_alarm_free():
    assert run("c.alarmSummary(c.normalize({color:1,diagnostics_complete:true}))") == "Причина Error не определена"
    assert run("c.alarmSummary(c.normalize({color:1,diagnostics_complete:false}))") == "Диагностика недоступна"
    assert run("c.alarmSummary(c.normalize({color:3,diagnostics_complete:true}))") == "Сигналы остановки не активны"
    raw = "{color:1,diagnostics_complete:true,causes:[{text:'Motor'}]}"
    assert run(f"c.alarmSummary(c.normalize({raw}))") == "Активные сигналы остановки"
    assert run("c.alarmSummary(c.normalize({color:1,diagnostics_complete:true,cause_verified:true,causes:[{text:'Motor',is_cause:true}]}))") == "Активные причины Error"
    assert run("c.alarmSummary(c.normalize({color:1,diagnostics_complete:true,cause_verified:true,causes:[{text:'Motor',is_cause:false}]}))") == "Активные сигналы остановки"
