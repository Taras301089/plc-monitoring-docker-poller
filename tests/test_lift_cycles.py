"""Циклы лифта EL300: чистый расчёт начала, конца, ожидания и диаграмм на придуманных данных (база не нужна)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "fastapi-api"))

from lift_cycles import LIFTS, find_cycles, initial_value, intervals, signal_intervals, summary  # noqa: E402

CFG = LIFTS["EL300"]
T0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
AT2 = "OPMode08_DB.EL300.At2nd"
TOP = "OPMode08_DB.FK300.AtTop"
REV = "ITL_GWM_DB.RevNewData"
G3 = "OPMode08_DB.EL300.Goto3rd"
G4 = "OPMode08_DB.EL300.Goto4th"


def s(sec):
    return T0 + timedelta(seconds=sec)


def build(changes, init=None):
    """Ряды всех сигналов: начальные значения 0 (или из init) и изменения {сигнал: [(секунда, значение)]}."""
    init = init or {}
    return {n: (init.get(n, False), [(s(t), v) for t, v in changes.get(n, [])]) for n in CFG["signals"]}


def test_normal_cycle_ok():
    ser = build({AT2: [(10, True), (30, False)], TOP: [(5, True)], G3: [(15, True), (20, False)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert len(c) == 1
    assert c[0]["start"] == s(10) and c[0]["end"] == s(15)
    assert c[0]["wait_s"] == 5 and c[0]["status"] == "ok"
    assert c[0]["last_signal"] == AT2 and c[0]["last_signal_ts"] == s(10)
    assert summary(c) == {"count": 1, "ok": 1, "problem": 0, "open": 0, "aborted": 0, "avg_wait_ok_s": 5.0}


def test_long_wait_problem_last_signal_revnewdata():
    ser = build({TOP: [(0, True)], AT2: [(10, True), (60, False)], REV: [(38, True), (45, False)], G3: [(40, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["status"] == "problem" and c[0]["wait_s"] == 30
    assert c[0]["last_signal"] == REV and c[0]["last_signal_ts"] == s(38)


def test_goto4th_ends_cycle():
    ser = build({AT2: [(10, True)], TOP: [(10, True)], G4: [(18, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["end"] == s(18) and c[0]["status"] == "ok"


def test_permit_in_same_poll_as_start_is_zero_wait():
    # как 15.10 в 16:25: сигналы GWM пришли заранее, вилы и Goto3rd включились в один опрос, затем лифт ушёл со 2-го
    ser = build({REV: [(2, True)], AT2: [(0, True), (13, False)], TOP: [(10, True)], G3: [(10, True), (18, False)]},
                init={AT2: False})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["status"] == "ok" and c[0]["wait_s"] == 0 and c[0]["end"] == s(10)
    assert c[0]["last_signal"] == TOP


def test_left_station_without_permit_aborted():
    ser = build({AT2: [(10, True), (25, False)], TOP: [(5, True)], G3: [(40, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["status"] == "aborted" and c[0]["end"] is None and c[0]["aborted_ts"] == s(25)
    assert c[0]["wait_s"] is None


def test_no_end_in_period_open():
    ser = build({AT2: [(3590, True)], TOP: [(3590, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert len(c) == 1 and c[0]["status"] == "open" and c[0]["end"] is None


def test_start_only_on_rising_edge_and_no_overlap():
    # «И» уже было 1 на начало периода: это не начало; повторный фронт AtTop до конца цикла новый цикл не открывает
    ser = build({TOP: [(20, False), (22, True), (30, False), (32, True)], G3: [(26, True), (27, False), (40, True)]},
                init={AT2: True, TOP: True})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert [x["start"] for x in c] == [s(22), s(32)]
    assert c[0]["end"] == s(26) and c[1]["end"] == s(40)


def test_initial_value_restore():
    assert initial_value(True, None, False) is True          # последнее изменение до окна
    assert initial_value(None, True, True) is False          # до окна изменений нет: противоположно первому после
    assert initial_value(None, False, False) is True
    assert initial_value(None, None, True) is True           # изменений нет вообще: текущее значение
    assert initial_value(None, None, None) is None
    # восстановленное значение 1 до окна даёт отрезок с начала окна
    iv = signal_intervals((initial_value(None, False, False), [(s(5), False), (s(8), True)]), s(0), s(20))
    assert iv == [[s(0), s(5)], [s(8), s(20)]]


def test_intervals_window():
    ser = build({AT2: [(10, True), (30, False)], TOP: [(5, True)], G3: [(15, True), (20, False)]})
    c = find_cycles(ser, CFG, T0, T1, 10)[0]
    iv = intervals(ser, CFG["signals"], c)
    assert list(iv) == CFG["signals"]
    assert iv[AT2] == [[s(10), s(30)]]
    assert iv[TOP] == [[s(5), s(45)]]                        # окно: начало-15 с .. конец+30 с
    assert iv[G3] == [[s(15), s(20)]] and iv[G4] == []
