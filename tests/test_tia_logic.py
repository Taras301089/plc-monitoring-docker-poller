"""Разбор логики ПЛК из выгрузки TIA (S-7): временные синтетические сети и, если есть, настоящая выгрузка WBS1."""
import sys
from pathlib import Path

import pytest

from synthetic_tia_fixtures import materialize_tia_fixtures

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fastapi-api"))
import tia_logic as tl  # noqa: E402

WBS1_ZIPS = sorted(Path(r"C:\tia-bridge\out").glob("TIA_wbs1_*.zip")) if Path(r"C:\tia-bridge\out").exists() else []


@pytest.fixture
def mini(tmp_path):
    return tl.TiaLogic.load(materialize_tia_fixtures(tmp_path / "tia_logic", "tia_logic"))


def one(logic, name, **kw):
    r = logic.chains_for(name, **kw)
    assert len(r) == 1, r
    return r[0]


def test_series_parallel_not_and_quoting(mini):
    e = one(mini, "DB1.Motor")
    assert (e["block"], e["net"], e["k"]) == ("Mini_FC", 1, "coil")
    assert e["title"] == "Пуск двигателя"
    # (Sens A ИЛИ Self) И НЕ Blocked; имя с пробелом в кавычках как в TIA
    assert tl.to_text(e["c"]) == '(DB1."Sens A" ИЛИ DB1.Self) И НЕ DB1.Blocked'
    assert e["c"]["and"][0]["or"][0] == {"v": 'DB1."Sens A"'}
    assert e["c"]["and"][1] == {"v": "DB1.Blocked", "n": 1}


def test_search_ignores_quotes_and_case(mini):
    assert one(mini, "db1.MOTOR")["o"] == "DB1.Motor"
    assert mini.chains_for("Motor") == [] and len(mini.chains_for("Motor", partial=True)) == 1


def test_compare_set_reset_and_unparsed(mini):
    r = mini.chains_for("DB1.Full")
    assert [x["k"] for x in r] == ["set", "reset"]
    assert tl.to_text(r[0]["c"]) == "(DB1.Level >= 50) И DB1.Ready"
    assert tl.to_text(r[1]["c"]) == "DB1.Reset"
    # Calc не разбирается и об этом сказано явно, а не выдумано
    assert one(mini, "DB1.Total")["v"] == {"unparsed": "Calc"}
    assert mini.stats()["unparsed"] == {"Calc": 1}


def test_timer_condition(mini):
    e = one(mini, "DB1.Done")
    assert tl.to_text(e["c"]) == "TON(DB1.TonRun: DB1.Running, T#300MS) И DB1.Delay"
    assert [x["k"] for x in mini.chains_for("DB1.TonRun")] == ["timer"]


def test_call_without_and_with_expansion(mini):
    e = one(mini, "DB1.Running")
    call = e["call"]
    assert (call["blk"], call["inst"], call["p"]) == ("Sub_FB", "Sub_FB_IDB", "Run")
    assert call["args"]["Start"] == {"v": "DB1.Go"} and call["args"]["Block"] == {"v": "DB1.Alarm"}
    assert "body" not in call
    deep = one(mini, "DB1.Running", depth=1)["call"]["body"]
    assert len(deep) == 1 and deep[0]["k"] == "coil"
    # параметры подставлены фактическими: Start -> DB1.Go, Block -> DB1.Alarm, глобальная DB1.Estop как есть
    assert tl.to_text(deep[0]["c"]) == "DB1.Go И НЕ DB1.Alarm И НЕ DB1.Estop"


def test_instance_output_resolves_through_fb(mini):
    r = mini.chains_for("Sub_FB_IDB.Run")
    assert len(r) == 1 and r[0]["block"] == "Sub_FB"
    assert tl.to_text(r[0]["c"]) == "DB1.Go И НЕ DB1.Alarm И НЕ DB1.Estop"


def test_json_roundtrip_is_compact(mini):
    import json

    data = json.loads(mini.to_json())
    again = tl.TiaLogic.from_json(data)
    assert tl.to_text(one(again, "DB1.Motor")["c"]) == tl.to_text(one(mini, "DB1.Motor")["c"])
    assert ": " not in mini.to_json()[:200]      # без лишних пробелов


def test_missing_output_gives_empty(mini):
    assert mini.chains_for("Нет.Такого.Выхода") == []


@pytest.mark.skipif(not WBS1_ZIPS, reason="нет выгрузки WBS1 в C:\\tia-bridge\\out")
def test_wbs1_lift_el300_goto3rd():
    logic = tl.TiaLogic.load(WBS1_ZIPS[-1])
    r = logic.chains_for("OPMode08_DB.EL300.Goto3rd")
    coils = [e for e in r if e["block"] == "ST01_EL300" and e["k"] == "coil"]
    assert len(coils) == 1
    c = coils[0]["c"]
    assert coils[0]["o"] == "OPMode08_DB.EL300.Goto3rd"
    # верхний уровень: (ветки At2nd/Goto3rd ... ) И FK300.Nobody И НЕ EL300.At3rd
    top = c["and"]
    assert {"v": "OPMode08_DB.FK300.Nobody"} in top
    assert {"v": "OPMode08_DB.EL300.At3rd", "n": 1} in top
    leaves = tl.leaf_names(c)
    for need in ("OPMode08_DB.EL300.At2nd", "OPMode08_DB.EL300.Goto3rd", "OPMode08_DB.FK300.AtTop",
                 'ITL_GWM_DB."From GWM".BodyCheck1', "ITL_GWM_DB.RevNewData", 'ITL_GWM_DB."From GWM".Auto'):
        assert need in leaves, need
    # самоподхват: Goto3rd входит в собственное условие
    assert "OPMode08_DB.EL300.Goto3rd" in leaves
    # блок FC650 и номер
    assert logic.blocks["ST01_EL300"]["num"] == 650
    # нераспознанное явно посчитано
    assert logic.stats()["unparsed"].get("Calc", 0) > 0
    assert logic.stats()["blocks"]["FC"] > 100
