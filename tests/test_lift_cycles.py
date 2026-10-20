"""Циклы лифта EL300: чистый расчёт начала, конца, ожидания и диаграмм на придуманных данных (база не нужна)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "fastapi-api"))

from lift_cycles import LIFTS, all_signals, find_cycles, initial_value, intervals, signal_intervals, summary, view_window  # noqa: E402

CFG = LIFTS["EL300"]
T0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
AT2 = "OPMode08_DB.EL300.At2nd"
TOP = "OPMode08_DB.FK300.AtTop"
REV = "ITL_GWM_DB.RevNewData"
G3 = "OPMode08_DB.EL300.Goto3rd"
G4 = "OPMode08_DB.EL300.Goto4th"
AT3 = "OPMode08_DB.EL300.At3rd"          # прибыл на 3-й: команда Goto3rd пропадает в тот же опрос (иначе «Остановка в пути»)


def s(sec):
    return T0 + timedelta(seconds=sec)


def build(changes, init=None):
    """Ряды всех сигналов: начальные значения 0 (или из init) и изменения {сигнал: [(секунда, значение)]}."""
    init = init or {}
    return {n: (init.get(n, False), [(s(t), v) for t, v in changes.get(n, [])]) for n in all_signals(CFG)}


def test_normal_cycle_ok():
    ser = build({AT2: [(10, True), (30, False)], TOP: [(5, True)], G3: [(15, True), (20, False)], AT3: [(20, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert len(c) == 1
    assert c[0]["start"] == s(10) and c[0]["end"] == s(15)
    assert c[0]["wait_s"] == 5 and c[0]["status"] == "ok"
    assert c[0]["last_signal"] == AT2 and c[0]["last_signal_ts"] == s(10)
    assert summary(c) == {"count": 1, "ok": 1, "problem": 0, "stuck": 0, "open": 0, "aborted": 0, "avg_wait_ok_s": 5.0}


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
    ser = build({REV: [(2, True)], AT2: [(0, True), (13, False)], TOP: [(10, True)], G3: [(10, True), (18, False)],
                 AT3: [(18, True)]}, init={AT2: False})
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
    ser = build({AT2: [(10, True), (30, False)], TOP: [(5, True)], G3: [(15, True), (20, False)], AT3: [(20, True)]})
    c = find_cycles(ser, CFG, T0, T1, 10)[0]
    iv = intervals(ser, CFG["signals"], c)
    assert list(iv) == CFG["signals"]
    assert iv[AT2] == [[s(10), s(30)]]
    assert iv[TOP] == [[s(5), s(45)]]                        # окно: начало-15 с .. конец+30 с
    assert iv[G3] == [[s(15), s(20)]] and iv[G4] == []


# ---------- «Остановка в пути» и начало цикла, устойчивое к опросу (реальный случай 16.10.2026, EL300) ----------

RUN = "OPMode08_DB.EL300.ORunning"
REQ = "ITL_GWM_DB.From GWM.ReqRevCar"
SHT = "ITL_GWM_DB.From GWM.SHT_Inposition"
BC1 = "ITL_GWM_DB.From GWM.BodyCheck1"
RBI = "ITL_GWM_DB.From GWM.RB_Inposition"
POS = "ITL_GWM_DB.From GWM.POS_Closed"
PT = timezone(timedelta(hours=5))          # время завода
D0 = datetime(2026, 10, 16, 12, 40, 0, tzinfo=PT)
D1 = datetime(2026, 10, 16, 13, 30, 0, tzinfo=PT)


def at(hms):
    """«12:53:32.542» -> момент 16.10.2026 по времени завода."""
    h, m, sec = hms.split(":")
    return datetime(2026, 10, 16, int(h), int(m), 0, tzinfo=PT) + timedelta(seconds=float(sec))


def real(changes, init):
    """Ряды всех сигналов лифта (включая уровни): начальные значения init, изменения {сигнал: [(«ЧЧ:ММ:СС.мс», v)]}."""
    return {n: (init.get(n, False), [(at(t), v) for t, v in changes.get(n, [])]) for n in all_signals(CFG)}


REAL_INIT = {AT2: True, "ITL_GWM_DB.From GWM.Auto": True, "OPMode08_DB.FK300.Nobody": True}
REAL = {
    # нормальный цикл 12:49
    REQ: [("12:49:06.000", True), ("12:49:39.000", False), ("12:53:20.894", True), ("12:53:38.560", False)],
    TOP: [("12:49:17.105", True), ("12:49:25.473", False), ("12:53:32.542", True), ("12:57:52.072", False)],
    G3: [("12:49:17.105", True), ("12:49:25.473", False), ("12:53:32.542", True), ("12:53:38.560", False)],
    RUN: [("12:49:17.105", True), ("12:49:25.473", False), ("12:53:32.542", True), ("12:53:38.560", False),
          ("12:57:43.577", True), ("12:57:46.337", False)],
    AT2: [("12:49:19.940", False), ("12:51:00.000", True), ("12:53:32.542", False)],
    AT3: [("12:49:25.473", True), ("12:50:30.000", False), ("12:57:46.337", True)],
    # остановка 12:53
    SHT: [("12:53:06.245", True)],
    BC1: [("12:53:12.068", True)],
    RBI: [("12:53:17.953", True)],
    POS: [("12:53:20.894", True)],
    REV: [("12:53:20.894", True)],
}


def test_real_stop_between_levels_is_one_stuck():
    c = find_cycles(real(REAL, REAL_INIT), CFG, D0, D1, 10)
    assert [x["start"] for x in c] == [at("12:49:17.105"), at("12:53:32.542")]
    assert c[0]["status"] == "ok" and c[0]["stop_ts"] is None
    st = c[1]
    assert st["status"] == "stuck" and [x["status"] for x in c].count("stuck") == 1
    assert st["end"] == at("12:53:32.542") and st["wait_s"] == 0
    assert st["stop_ts"] == at("12:53:38.560") and st["resume_ts"] == at("12:57:46.337")
    assert abs(st["stop_s"] - 247.777) < 0.01
    assert st["lost_signal"] == REQ and st["lost_signal_ts"] == at("12:53:38.560")
    assert st["forks_out"] is True
    sm = summary(c)
    assert sm["stuck"] == 1 and sm["ok"] == 1 and sm["count"] == 2


def test_stuck_view_window_and_orunning_intervals():
    ser = real(REAL, REAL_INIT)
    st = find_cycles(ser, CFG, D0, D1, 10)[1]
    w0, w1 = view_window(st)
    assert w0 == st["start"] - timedelta(seconds=15) and w1 == st["resume_ts"] + timedelta(seconds=30)
    iv = intervals(ser, CFG["signals"], st)
    assert RUN in CFG["signals"]
    assert iv[RUN] == [[at("12:53:32.542"), at("12:53:38.560")], [at("12:57:43.577"), at("12:57:46.337")]]


def test_stuck_view_window_not_longer_than_10_min():
    st = {"start": s(100), "end": s(100), "aborted_ts": None, "status": "stuck", "stop_ts": s(106), "resume_ts": s(2000)}
    w0, w1 = view_window(st)
    assert (w1 - w0).total_seconds() == 600


def test_at2nd_falls_in_same_poll_as_forks_arrives_in_time_ok():
    ser = build({TOP: [(10, True), (30, False)], AT2: [(10, False)], G3: [(10, True), (18, False)],
                 RUN: [(10, True), (18, False)], AT3: [(18, True)]}, init={AT2: True})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert len(c) == 1 and c[0]["start"] == s(10) and c[0]["status"] == "ok" and c[0]["wait_s"] == 0
    assert c[0]["stop_ts"] is None


def test_short_stop_within_tolerance_not_stuck():
    # разрешение пропало, уровень пришёл через 3 с (допуск 5 с): не остановка
    ser = build({TOP: [(10, True)], AT2: [(10, False)], G3: [(10, True), (18, False)], AT3: [(21, True)]}, init={AT2: True})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["status"] == "ok"
    assert find_cycles(ser, CFG, T0, T1, 10, min_stop_s=2)[0]["status"] == "stuck"


def test_not_arrived_until_period_end_stuck_without_resume():
    ser = build({TOP: [(10, True)], AT2: [(10, False)], G3: [(10, True), (18, False)], REQ: [(5, True), (18, False)]},
                init={AT2: True})
    c = find_cycles(ser, CFG, T0, T1, 10)
    assert c[0]["status"] == "stuck" and c[0]["resume_ts"] is None and c[0]["stop_ts"] == s(18)
    assert c[0]["stop_s"] == 3600 - 18 and c[0]["lost_signal"] == REQ and c[0]["forks_out"] is True
