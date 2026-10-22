"""Справочник аварий ProDiag из выгрузки TIA: подстановка "#операнда" в абсолютный адрес OPC UA (база и ПЛК не нужны)."""
import sys
from pathlib import Path

import pytest

from synthetic_tia_fixtures import materialize_tia_fixtures

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "fastapi-api"))

import prodiag_catalog as pc  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    fixture_dir = materialize_tia_fixtures(tmp_path / "prodiag", "prodiag")

    def build(general=None):
        return pc.build_catalog(pc.load_blocks(fixture_dir), general)

    return build


def _rows(records, instance, number):
    return [r for r in records if r.instance == instance and r.number == number]


def test_split_operand_quotes_and_indexes():
    local, parts = pc.split_operand('#Fault."A b"[3].x')
    assert local is True
    assert [(p.name, p.index) for p in parts] == [("Fault", []), ("A b", [3]), ("x", [])]
    local, parts = pc.split_operand('"PN&Hardware_DB".ET200S."MCP-GW01"')
    assert local is False and [p.name for p in parts] == ["PN&Hardware_DB", "ET200S", "MCP-GW01"]


def test_global_instance_db_operand(catalog):
    # FB вызван с экземплярным DB "Dev_DB": #IN → ns=3;s="Dev_DB"."IN"
    r = _rows(catalog(), "Dev_DB", 1)[0]
    assert r.node == 'ns=3;s="Dev_DB"."IN"'
    assert r.status is False and r.delay_ms == 1500
    assert r.text.startswith("Device ready")


def test_multi_instance_expands_every_parent_call(catalog):
    # Parent_FB вызван дважды (Parent_DB1, Parent_DB2), внутри мультиэкземпляр Dev1
    recs = catalog()
    insts = sorted({r.instance for r in recs if r.source == "Dev_FB"})
    assert insts == ["Dev_DB", "Parent_DB1.Dev1", "Parent_DB2.Dev1"]
    r = _rows(recs, "Parent_DB2.Dev1", 2)[0]
    assert r.node == 'ns=3;s="Parent_DB2"."Dev1"."St"."A b"'
    assert r.text.startswith("Датчик")


def test_array_index_counts_from_zero(catalog):
    # Arr: Array[1..4]; условие #Arr[2] → узел [1]
    r = _rows(catalog(), "Parent_DB1.Dev1", 1)[0]
    assert r.conditions == [('ns=3;s="Parent_DB1"."Dev1"."Arr"[1]', True)]


def test_global_condition_and_constants_skipped(catalog):
    # "Glob_DB".x[3] (Array[1..8]) → [2]; условие-константа TRUE и пустые условия пропускаются
    r = _rows(catalog(), "Dev_DB", 2)[0]
    assert r.conditions == [('ns=3;s="Glob_DB"."x"[2]', False)]
    assert r.sv_type == "MessageError" and r.status is True


def test_actual_parameter_and_call_constants(catalog):
    # вход IN мультиэкземпляра подключён к "Sig_DB".s1; константа Name подставляется в текст @4%s@
    r = _rows(catalog(), "Parent_DB1.Dev1", 1)[0]
    assert r.actual == 'ns=3;s="Sig_DB"."s1"'
    assert "Press 1 not ready" in r.text


def test_closed_member_is_noted(catalog):
    r = _rows(catalog(), "Dev_DB", 3)[0]
    assert "ExternalAccessible" in r.note
    assert "ExternalAccessible" not in _rows(catalog(), "Dev_DB", 1)[0].note


def test_general_pd_global_operand(catalog):
    row = {"fb": "General_PD", "id": 71, "tag": '"Glob_DB".ok', "status": False, "type": "Operand",
           "category": 1, "sub1": 0, "sub2": 0, "delay": "T#0ms",
           "conditions": [("", True), ('"Glob_DB".x[1]', True), ("", True)], "text": ""}
    r = [x for x in catalog([row]) if x.source == "General_PD"][0]
    assert r.node == 'ns=3;s="Glob_DB"."ok"'
    assert r.conditions == [('ns=3;s="Glob_DB"."x"[0]', True)]
    assert r.text == "Network OK" and r.delay_ms == 0


def test_delay_parsing():
    assert pc.delay_ms("T#0ms") == 0
    assert pc.delay_ms("T#500ms") == 500
    assert pc.delay_ms("T#2m_3s") == 123000
    assert pc.delay_ms("#CFG_DELAY") is None


def test_const_text():
    assert pc.const_text("WSTRING#'RB104 check car'") == "RB104 check car"
    assert pc.const_text("'MCP_ESTOP'") == "MCP_ESTOP"


def test_read_node_falls_back_to_actual_parameter(catalog):
    # открытый сигнал читается сам; закрытый (ExternalAccessible=false) без подключения — читать нечего
    recs = catalog()
    assert _rows(recs, "Dev_DB", 1)[0].read_node == 'ns=3;s="Dev_DB"."IN"'
    assert _rows(recs, "Dev_DB", 3)[0].read_node == ""
