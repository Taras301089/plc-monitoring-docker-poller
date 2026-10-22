"""Ответы киповцам «почему стоит» и «что за авария» (S-8): только поддельные деревья, значения и аварии; ни ПЛК, ни рабочей базы."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fastapi-api"))
import tia_logic as tl  # noqa: E402
import why  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
OUT = "DB.Out"


def v(name, n=False):
    return {"v": name, **({"n": 1} if n else {})}


def logic_with(cond, extra_nets=()):
    nets = [{"n": 1, "w": [{"o": OUT, "k": "coil", "c": cond}]}, *extra_nets]
    return tl.TiaLogic({"B": {"name": "B", "num": 1, "type": "FC", "lang": "LAD", "if": {}, "nets": nets}})


def out(cond, vals, **kw):
    vals = {OUT: False, **vals}
    return asyncio.run(why.explain_output(1, OUT, logic=logic_with(cond), values=vals, since=kw.pop("since", {}),
                                          names=kw.pop("names", {}), now=NOW, **kw))


def lines(text):
    return [l for l in text.splitlines() if l.strip()]


# ---------- тип А: вычисление дерева ----------
def test_and_one_condition_false():
    c = {"and": [v("DB.A"), v("DB.B"), v("DB.C")]}
    r = out(c, {"DB.A": True, "DB.B": False, "DB.C": True})
    assert r["state"] == "blocked"
    assert [m["tech_name"] for m in r["missing"]] == ["DB.B"] and r["missing"][0]["need"] is True
    assert (r["satisfied"], r["total"]) == (2, 3)


def test_or_two_false_branches_picks_closest():
    far = {"and": [v("DB.A"), v("DB.B"), v("DB.C")]}      # ложных 2
    near = {"and": [v("DB.D"), v("DB.E")]}                # ложный 1
    r = out({"or": [far, near]}, {"DB.A": True, "DB.B": False, "DB.C": False, "DB.D": True, "DB.E": False})
    assert [m["tech_name"] for m in r["missing"]] == ["DB.E"]
    assert (r["satisfied"], r["total"]) == (1, 2)


def test_or_with_true_branch_is_satisfied():
    c = {"and": [{"or": [v("DB.A"), v("DB.B")]}, v("DB.C")]}
    r = out(c, {"DB.A": False, "DB.B": True, "DB.C": False})
    assert [m["tech_name"] for m in r["missing"]] == ["DB.C"]


def test_not_contact_must_be_off():
    r = out({"and": [v("DB.A"), v("DB.Fault", n=True)]}, {"DB.A": True, "DB.Fault": True})
    assert r["missing"][0]["tech_name"] == "DB.Fault" and r["missing"][0]["need"] is False
    assert "Мешает" in why.render_simple_output(r)


def test_compare_condition():
    c = {"and": [{"cmp": ">=", "a": v("DB.Level"), "b": {"k": "50"}}, v("DB.Ready")]}
    assert out(c, {"DB.Level": 30, "DB.Ready": True})["missing"][0]["tech_name"] == "DB.Level"
    assert out(c, {"DB.Level": 60, "DB.Ready": False})["missing"][0]["tech_name"] == "DB.Ready"


def test_unreadable_leaf_gives_unknown_not_a_guess():
    c = {"and": [v("DB.A"), v("DB.B")]}
    r = out(c, {"DB.A": True})                   # значения B нет
    assert r["state"] == "unknown" and r["unread"] == ["DB.B"]
    assert "не разобрана" in why.render_simple_output(r)


def test_unparsed_leaf_gives_unknown():
    r = out({"and": [v("DB.A"), {"unparsed": "Calc"}]}, {"DB.A": True})
    assert r["state"] == "unknown" and r["unparsed"] == 1


def test_unparsed_with_other_false_still_blocked_and_marked():
    r = out({"and": [v("DB.A"), {"unparsed": "Calc"}]}, {"DB.A": False})
    assert r["state"] == "blocked" and r["unparsed"] == 1
    assert "часть логики не разобрана" in why.render_simple_output(r)


def test_output_already_on_is_ok():
    r = out(v("DB.A"), {"DB.A": False, OUT: True})
    assert r["state"] == "ok" and "Всё в порядке" in why.render_simple_output(r)


def test_conditions_met_but_output_off_is_unknown():
    r = out(v("DB.A"), {"DB.A": True})
    assert r["state"] == "unknown" and "выход выключен" in r["note"]


def test_unknown_output_name():
    r = asyncio.run(why.explain_output(1, "DB.Nope", logic=logic_with(v("DB.A")), values={}, names={}, since={}, now=NOW))
    assert r["state"] == "unknown" and "не найдена" in r["note"]


def test_call_body_is_expanded_and_unexpanded_call_is_unparsed():
    body = [{"net": 1, "k": "coil", "o": "Ok", "c": v("DB.Inner")}]
    node = {"call": {"blk": "Sub", "p": "Ok", "args": {}, "body": body}}
    r = out({"and": [node, v("DB.A")]}, {"DB.A": True, "DB.Inner": False})
    assert r["missing"][0]["tech_name"] == "DB.Inner"
    r = out({"and": [{"call": {"blk": "Sub", "p": "Ok", "args": {}}}, v("DB.A")]}, {"DB.A": True})
    assert r["state"] == "unknown" and r["unparsed"] == 1


def test_timer_uses_instance_output():
    c = {"and": [{"timer": "TON", "inst": "DB.T1", "in": v("DB.A"), "pt": {"k": "T#5S"}}, v("DB.B")]}
    r = out(c, {"DB.T1.Q": False, "DB.B": True})
    assert r["missing"][0]["kind"] == "timer"


def test_names_are_matched_without_quotes_and_case():
    c = v('DB."Sens A"')
    r = out(c, {'db.sens a': False}, names={'DB.Sens A': ("Датчик А", "на пульте")})
    assert r["missing"][0]["simple_name"] == "Датчик А" and r["missing"][0]["place"] == "на пульте"


# ---------- тип А: чтение значений ----------
def test_live_read_is_one_request_and_timeout_gives_unknown():
    calls = []

    async def slow(plc_id, node_ids):
        calls.append(node_ids)
        await asyncio.sleep(5)
        return {}

    r = asyncio.run(why.explain_output(1, OUT, logic=logic_with({"and": [v("DB.A"), v("DB.B")]}), names={}, since={},
                                       reader=slow, timeout=0.05, now=NOW))
    assert r["state"] == "unknown" and "не ответил" in r["note"]
    assert len(calls) == 1 and len(calls[0]) == 3          # A, B и сам выход одним запросом


def test_live_read_error_means_no_numbers():
    async def broken(plc_id, node_ids):
        raise ConnectionError("down")

    r = asyncio.run(why.explain_output(1, OUT, logic=logic_with(v("DB.A")), names={}, since={}, reader=broken, now=NOW))
    assert r["state"] == "unknown" and r["note"] == "нет связи с ПЛК" and "нет связи" in why.render_simple_output(r).lower()


def test_live_values_parsed_from_poller_answer():
    async def ok(plc_id, node_ids):
        return {'ns=3;s="DB"."A"': {"ok": True, "v": True}, 'ns=3;s="DB"."B"': {"ok": True, "v": False},
                'ns=3;s="DB"."Out"': {"ok": True, "v": False}}

    r = asyncio.run(why.explain_output(1, OUT, logic=logic_with({"and": [v("DB.A"), v("DB.B")]}), names={}, since={}, reader=ok, now=NOW))
    assert r["state"] == "blocked" and r["missing"][0]["tech_name"] == "DB.B"


def test_node_id_for_quotes_parts_and_keeps_index():
    assert why.node_id_for('ITL_GWM_DB."From GWM".BodyCheck1') == 'ns=3;s="ITL_GWM_DB"."From GWM"."BodyCheck1"'
    assert why.node_id_for("DB.Arr[3].X") == 'ns=3;s="DB"."Arr"[3]."X"'


# ---------- тексты ----------
def test_text_has_no_tech_names_when_simple_names_given():
    c = {"and": [v("DB.Req"), v("DB.Ready")]}
    names = {"DB.Req": ("запрос кузова от линии", "пульт линии"), "DB.Ready": ("готовность лифта", "шкаф лифта")}
    r = out(c, {"DB.Req": False, "DB.Ready": True}, names=names, since={"DB.Req": 260})
    t = why.render_simple_output(r)
    assert "DB." not in t and "запрос кузова от линии" in t and "Уже 4 мин 20 с." in t and "пульт линии" in t
    assert "Остальные условия выполнены (1 из 2)." in t
    assert len(lines(t)) <= 6


def test_text_without_simple_name_shows_tech_name_with_engineer_note():
    t = why.render_simple_output(out(v("DB.X"), {"DB.X": False}))
    assert "сигнал DB.X" in t and "Для инженера" in t and "Сколько времени так, неизвестно." in t


def test_many_missing_shows_two_and_rest_and_stays_within_six_lines():
    leaves = [v(f"DB.S{i}") for i in range(5)]
    names = {f"DB.S{i}": (f"сигнал {i}", f"место {i}") for i in range(5)}
    t = why.render_simple_output(out({"and": leaves}, {f"DB.S{i}": False for i in range(5)}, names=names))
    assert "и ещё 3" in t and len(lines(t)) <= 6


def test_all_render_variants_up_to_six_lines():
    c = {"and": [v("DB.A"), v("DB.F", n=True), {"unparsed": "Calc"}]}
    names = {"DB.A": ("а", "п1"), "DB.F": ("ф", "п2")}
    for vals in ({"DB.A": False, "DB.F": True}, {"DB.A": True, "DB.F": False}, {}):
        assert len(lines(why.render_simple_output(out(c, vals, names=names)))) <= 6


def test_fmt_dur():
    assert [why.fmt_dur(x) for x in (45, 260, 720, 7500, None)] == ["45 с", "4 мин 20 с", "12 мин", "2 ч 5 мин", ""]


# ---------- тип Б: аварии ----------
def alarm_logic():
    call = {"blk": "02Msg_Stn", "type": "FC", "args": {
        "MsgSource": v("DB.AirOk", n=True), "MsgPrecondition": v("SYS_Allways_ON")}}
    net = {"n": 2, "t": "UB020 air fault  MSG.W[60].x1", "calls": {"21": call}}
    other = {"n": 3, "t": "без адреса", "calls": {"22": dict(call)}}
    return tl.TiaLogic({"Alarm": {"name": "Alarm", "num": 1, "type": "FC", "lang": "LAD", "if": {}, "nets": [net, other]}})


BIT = 60 * 16 + 1
EVENTS = ([{"started_at": NOW - timedelta(minutes=12), "ended_at": None, "active_at_start": False}]
          + [{"started_at": NOW - timedelta(days=d, minutes=5), "ended_at": NOW - timedelta(days=d, minutes=2), "active_at_start": False}
             for d in (1, 2, 3, 4)])
HELP = {"simple": "Нет давления воздуха на станции", "causes": "", "fix": "Открыть кран воздуха\nПроверить реле давления"}
NAMES = {"DB.AirOk": ("Давление воздуха в норме", "реле давления на станции")}


def alarm(bit=BIT, **kw):
    base = dict(logic=alarm_logic(), values={"DB.AirOk": False, "SYS_Allways_ON": True}, since={"DB.AirOk": 720}, names=NAMES,
                catalog={"text": "UB020 air fault", "station": "UB020"}, help=HELP, events=EVENTS, now=NOW)
    base.update(kw)
    return asyncio.run(why.explain_alarm(4, bit, **base))


def test_alarm_source_found_by_address_in_title():
    assert set(why.alarm_calls(alarm_logic())) == {BIT}
    r = alarm()
    assert r["state"] == "found" and r["source"]["net"] == 2 and r["source_state"] == "holding"
    assert r["active_now"] and r["active_for_sec"] == 720
    assert [s["tech_name"] for s in r["active_signals"]] == ["DB.AirOk"] and r["active_signals"][0]["state"] is False
    assert r["history"] == {"count_7d": 5, "typical_sec": 180, "last_ended_at": (NOW - timedelta(days=1, minutes=2)).isoformat()}


def test_alarm_text_full_answer():
    t = why.render_simple_alarm(alarm())
    assert "Нет давления воздуха на станции" in t and "Давление воздуха в норме (сигнала нет), уже 12 мин." in t
    assert "Где смотреть: реле давления на станции." in t and "Открыть кран воздуха; Проверить реле давления" in t
    assert "За 7 дней было 5 раз, обычно снимается за 3 мин." in t
    assert "DB." not in t and len(lines(t)) <= 6


def test_alarm_without_help_says_no_instruction():
    t = why.render_simple_alarm(alarm(help=None))
    assert "инструкции пока нет" in t and "UB020 air fault" in t


def test_alarm_source_not_found_is_unknown_not_guessed():
    r = alarm(bit=5)
    assert r["state"] == "unknown" and r["source"] is None and "не найден" in r["note"]
    t = why.render_simple_alarm(r)
    assert "не удалось" in t and len(lines(t)) <= 6


def test_alarm_cause_gone_waits_for_reset():
    r = alarm(values={"DB.AirOk": True, "SYS_Allways_ON": True})
    assert r["source_state"] == "gone" and "ждёт сброса" in why.render_simple_alarm(r)


def test_alarm_not_active_and_no_history():
    t = why.render_simple_alarm(alarm(events=[]))
    assert "Сейчас авария не активна." in t and "За 7 дней такого не было." in t


def test_alarm_unreadable_values_is_not_a_guess():
    r = alarm(values={})
    assert r["source_state"] == "unknown" and r["unread"] and not r["active_signals"]
    assert len(lines(why.render_simple_alarm(r))) <= 6


def test_alarm_no_logic_loaded():
    r = alarm(logic=None)
    assert r["state"] == "unknown"


def test_history_ignores_alarms_seen_active_at_start_for_duration():
    ev = [{"started_at": NOW - timedelta(days=1), "ended_at": NOW - timedelta(days=1) + timedelta(hours=5), "active_at_start": True},
          {"started_at": NOW - timedelta(days=2), "ended_at": NOW - timedelta(days=2) + timedelta(minutes=4), "active_at_start": False},
          {"started_at": NOW - timedelta(days=9), "ended_at": NOW - timedelta(days=9) + timedelta(minutes=1), "active_at_start": False}]
    h = why.history_stats(ev, NOW)
    assert h["count_7d"] == 2 and h["typical_sec"] == 240


# ---------- API ----------
async def test_api_requires_login_and_answers_without_logic(users, anon, pool):
    assert (await anon.get("/api/why/output", params={"plc_id": 1, "output": "X"})).status_code == 401
    assert (await anon.get("/api/why/alarm", params={"plc_id": 1, "bit": 0})).status_code == 401
    r = await users["t_viewer"].get("/api/why/output", params={"plc_id": 999, "output": "DB.X"})
    assert r.status_code == 200 and r.json()["result"]["state"] == "unknown" and "Точную причину" in r.json()["text"]
    r = await users["t_viewer"].get("/api/why/alarm", params={"plc_id": 999, "bit": 5})
    assert r.status_code == 200 and r.json()["result"]["state"] == "unknown" and r.json()["text"]
    assert (await users["t_viewer"].get("/api/why/alarm", params={"plc_id": 999})).status_code == 422


async def test_api_reads_alarm_tables_and_survives_missing_help_tables(users, pool):
    """Таблиц alarm_help и signal_names в этой ветке может не быть (S-12): ответ строится без них."""
    plc = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ('why1', 'opc.tcp://10.0.0.1:4840') RETURNING id")
    await pool.execute("INSERT INTO alarm_catalog (plc_id, bit_index, word_index, bit_in_word, text) VALUES ($1, 5, 0, 5, 'Тестовая авария')", plc)
    await pool.execute("INSERT INTO alarm_events (plc_id, bit_index, started_at, ended_at) VALUES ($1, 5, now() - interval '1 day', now() - interval '1 day' + interval '3 minutes')", plc)
    res = await why.explain_alarm(plc, 5, pool=pool, logic=alarm_logic(), values={})
    assert res["alarm_text"] == "Тестовая авария" and res["history"]["count_7d"] == 1 and res["help"] is None
    assert "инструкции пока нет" in why.render_simple_alarm(res)
