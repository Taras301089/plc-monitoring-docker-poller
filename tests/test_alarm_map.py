"""Карта аварий ПЛК (S-14): имена вызываемых блоков в SCL/STL, счётчик слова и бита G.Err_Word/G.Err_Bit, источник аварии.

Синтетика: маленькие блоки в XML (разбор вызовов) и логика проекта из двух станций (порядок бит, типы, станции, защёлка, цикл, подпись сети).
Настоящая выгрузка GWM_UB: если файла нет, тест пропускается. База не нужна."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fastapi-api"))
import alarm_map as am  # noqa: E402
import tia_logic as tl  # noqa: E402

CATALOG = Path(__file__).resolve().parent / "fixtures" / "alarm_map" / "gwm_ub_catalog.tsv"
GWM_ZIP = Path(r"C:\tia-bridge\out\TIA_gwm_ub_2026-10-20_13-49.zip")

# ---------- XML: имена вызываемых блоков ----------
NS_ST = "http://www.siemens.com/automation/Openness/SW/NetworkSource/StructuredText/v3"
NS_STL = "http://www.siemens.com/automation/Openness/SW/NetworkSource/StatementList/v4"
CONST = '<Access Scope="LiteralConstant"><Constant><ConstantType>Int</ConstantType><ConstantValue>{}</ConstantValue></Constant></Access>'


def _block(kind: str, name: str, members: str, nets: str, lang: str = "SCL") -> str:
    return (f'<Document><SW.Blocks.{kind} ID="0"><AttributeList><Name>{name}</Name><Number>7</Number><ProgrammingLanguage>{lang}</ProgrammingLanguage>'
            f'<Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5"><Section Name="Input" />'
            f'<Section Name="Static">{members}</Section></Sections></Interface></AttributeList><ObjectList>{nets}</ObjectList></SW.Blocks.{kind}></Document>')


def _net(body: str, scl: bool = True) -> str:
    src = (f'<StructuredText xmlns="{NS_ST}">{body}</StructuredText>' if scl else f'<StatementList xmlns="{NS_STL}">{body}</StatementList>')
    return (f'<SW.Blocks.CompileUnit ID="1" CompositionName="CompileUnits"><AttributeList><NetworkSource>{src}</NetworkSource>'
            f'<ProgrammingLanguage>{"SCL" if scl else "STL"}</ProgrammingLanguage></AttributeList><ObjectList /></SW.Blocks.CompileUnit>')


def _glob(*names: str) -> str:
    comps = '<Token Text="." />'.join(f'<Component Name="{n}" />' for n in names)
    return f'<Access Scope="GlobalVariable"><Symbol>{comps}</Symbol></Access>'


SCL_CALLS = (
    '<Access Scope="Call" UId="21"><CallInfo UId="22" BlockType="FC"><Instance Scope="GlobalVariable"><Component Name="02Msg_Stn" /></Instance>'
    '<Token Text="(" /><Parameter Name="CFG_MsgType"><Token Text=":=" />' + _glob("Msg_ErrType", "Sonser") + '</Parameter><Token Text="," />'
    '<Parameter Name="MsgSource"><Token Text=":=" /><Access Scope="LocalVariable"><Symbol><Component Name="Fault" /></Symbol></Access></Parameter>'
    '<Token Text=")" /></CallInfo></Access><Token Text=";" />'
    '<Access Scope="Call"><CallInfo BlockType="FB"><Instance Scope="GlobalVariable"><Component Name="OBJ_DB" /></Instance><Token Text="(" />'
    '<Parameter Name="OBJ_NO"><Token Text=":=" />' + CONST.format(5) + '</Parameter><Token Text=")" /></CallInfo></Access><Token Text=";" />'
    '<Token Text="IF" /><Blank Num="1" /><Access Scope="LocalVariable"><Symbol><Component Name="Fault" /></Symbol></Access><Token Text="THEN" />'
    '<Access Scope="Call"><CallInfo BlockType="FC"><Instance Scope="GlobalVariable"><Component Name="01Msg_Cell" /></Instance><Token Text="(" />'
    '<Token Text=")" /></CallInfo></Access><Token Text=";" /><Token Text="END_IF" /><Token Text=";" />'
    '<Token Text="FOR" /><Blank Num="1" /><Access Scope="LocalVariable"><Symbol><Component Name="I" /></Symbol></Access><Token Text=":=" />'
    + CONST.format(1) + '<Token Text="TO" />' + CONST.format(3) + '<Token Text="DO" />'
    '<Access Scope="Call"><CallInfo BlockType="FC"><Instance Scope="GlobalVariable"><Component Name="02Msg_Stn" /></Instance><Token Text="(" />'
    '<Token Text=")" /></CallInfo></Access><Token Text=";" /><Token Text="END_FOR" /><Token Text=";" />')

STL_CALL = ('<StlStatement UId="24"><StlToken Text="CALL" /><Access Scope="Call"><CallInfo BlockType="FB"><Instance Scope="LocalVariable">'
            '<Component Name="OBJ1" /></Instance><Parameter Name="OBJ_NO" Section="Input" Type="Int">' + CONST.format(9) +
            '</Parameter><Parameter Name="Empty" Section="Input" Type="Int" /></CallInfo></Access></StlStatement>')


@pytest.fixture(scope="module")
def xml_logic(tmp_path_factory):
    d = tmp_path_factory.mktemp("xmlpj") / "PLC1" / "Blocks"
    d.mkdir(parents=True)
    (d / "Caller.xml").write_text(_block("FC", "Caller", "", _net(SCL_CALLS)), encoding="utf-8")
    (d / "Stn.xml").write_text(_block("FB", "Stn", '<Member Name="OBJ1" Datatype="&quot;ObjFb&quot;" />', _net(STL_CALL, scl=False), "STL"), encoding="utf-8")
    (d / "ObjFb.xml").write_text(_block("FB", "ObjFb", "", ""), encoding="utf-8")
    (d / "OBJ_DB.xml").write_text('<Document><SW.Blocks.InstanceDB ID="0"><AttributeList><InstanceOfName>ObjFb</InstanceOfName>'
                                  '<InstanceOfType>FB</InstanceOfType><Name>OBJ_DB</Name><Number>5</Number></AttributeList>'
                                  '</SW.Blocks.InstanceDB></Document>', encoding="utf-8")
    return tl.TiaLogic.load(d.parent.parent)


def test_scl_fc_name_comes_from_instance_and_args_are_mapped(xml_logic):
    calls = list(xml_logic.blocks["Caller"]["nets"][0]["calls"].values())
    first = calls[0]
    assert (first["blk"], first["type"], first["src"]) == ("02Msg_Stn", "FC", "SCL")
    assert first["args"]["CFG_MsgType"] == {"v": "Msg_ErrType.Sonser"}
    assert first["args"]["MsgSource"] == {"v": "Fault", "l": 1}
    assert "02Msg_Stn(CFG_MsgType" in first["txt"] and "Sonser" in first["txt"]


def test_scl_global_fb_instance_resolved_through_instance_db(xml_logic):
    assert xml_logic.idb == {"OBJ_DB": "ObjFb"}
    c = list(xml_logic.blocks["Caller"]["nets"][0]["calls"].values())[1]
    assert (c["blk"], c["inst"], c["type"]) == ("ObjFb", "OBJ_DB", "FB")
    assert c["args"]["OBJ_NO"] == {"k": "5"}
    assert xml_logic.to_dict()["idb"] == {"OBJ_DB": "ObjFb"}                      # попадает в JSON логики


def test_scl_condition_and_loop_flags(xml_logic):
    calls = list(xml_logic.blocks["Caller"]["nets"][0]["calls"].values())
    assert calls[2]["blk"] == "01Msg_Cell" and calls[2].get("cnd") == 1 and "loop" not in calls[2]
    assert calls[3]["loop"] == {"var": "I", "from": 1, "to": 3, "by": 1}
    assert "scl" in xml_logic.blocks["Caller"]["nets"][0]


def test_stl_multi_instance_type_from_interface(xml_logic):
    c = list(xml_logic.blocks["Stn"]["nets"][0]["calls"].values())[0]
    assert (c["blk"], c["inst"], c["loc"], c["src"]) == ("ObjFb", "OBJ1", 1, "STL")
    assert c["args"] == {"OBJ_NO": {"k": "9"}}                                     # пустой параметр не попадает в args


def test_old_json_without_new_fields_still_loads():
    old = {"plc": "P", "blocks": {"A": {"name": "A", "num": 1, "type": "FC", "lang": "LAD", "if": {}, "nets": []}}}
    assert tl.TiaLogic.from_json(old).blocks["A"]["nets"] == []


# ---------- логика проекта из двух станций (без XML) ----------
def blk(name, typ="FC", if_=None, nets=None):
    return {"name": name, "num": 1, "type": typ, "lang": "LAD", "if": if_ or {}, "nets": nets or []}


def msg(uid, typ, source, unit="G.stn_No", blkname="02Msg_Stn", pre=None, **kw):
    args = {"CFG_MsgType": {"v": f"Msg_ErrType.{typ}"}, "MsgSource": source, "MsgPrecondition": pre or {"v": "SYS_Allways_ON"}}
    args["CFG_Stn_No" if blkname == "02Msg_Stn" else "CFG_Cell_No"] = {"v": unit} if isinstance(unit, str) else {"k": str(unit)}
    return {uid: {"blk": blkname, "type": "FC", "args": args, **kw}}


def two_stations():
    obj = blk("ObjFb", "FB", {"Input": [["OBJ_NO", "Int"], ["SENS", "Bool"]], "Static": [["Fault", "Bool"], ["Timeout", "Bool"]]}, [
        {"n": 1, "t": "Latch", "w": [{"o": "Fault", "k": "set", "l": 1, "c": {"and": [{"v": "SENS", "l": 1}, {"v": "Timeout", "l": 1}]}},
                                     {"o": "Fault", "k": "reset", "l": 1, "c": {"v": "Operator.Reset"}}]},
        {"n": 2, "t": "Fault", "calls": msg("c1", "Sonser", {"v": "Fault", "l": 1})},
        {"n": 3, "t": "Second", "calls": msg("c1", "Sonser", {"v": "Gate.Open", "n": 1})}])
    total = blk("StnTotal", "FB", {"Input": [["STN_NO", "Int"], ["CFG_MsgWord", "Int"]]},
                [{"n": 1, "u": ["SCL"], "scl": "G.Err_Word := CFG_MsgWord;\nG.Err_Bit := 0;\nG.stn_No := STN_NO;"}])
    stn = blk("StnFb", "FB", {"Input": [["STN_NO", "Int"], ["WORD", "Int"], ["OBJ_NO", "Int"]], "Static": [["CFG", "StnTotal"], ["OBJ1", "ObjFb"], ["OBJ2", "ObjFb"]]}, [
        {"n": 1, "calls": {"c1": {"blk": "StnTotal", "type": "FB", "inst": "CFG", "loc": 1,
                                  "args": {"STN_NO": {"v": "STN_NO", "l": 1}, "CFG_MsgWord": {"v": "WORD", "l": 1}}}}},
        {"n": 2, "calls": {"c1": {"blk": "ObjFb", "type": "FB", "inst": "OBJ1", "loc": 1, "args": {"OBJ_NO": {"k": "1"}, "SENS": {"v": "IN_A"}}}}},
        {"n": 3, "calls": {"c1": {"blk": "ObjFb", "type": "FB", "inst": "OBJ2", "loc": 1, "args": {"OBJ_NO": {"k": "2"}, "SENS": {"v": "IN_B"}}}}},
        {"n": 4, "t": "Own", "calls": msg("c1", "Power", {"v": "Power.Lost"})}])
    stns = blk("Stns", "FC", {}, [
        {"n": 1, "calls": {"c1": {"blk": "StnFb", "type": "FB", "inst": "ST1_DB", "args": {"STN_NO": {"k": "1"}, "WORD": {"k": "10"}}}}},
        {"n": 2, "calls": {"c1": {"blk": "StnFb", "type": "FB", "inst": "ST2_DB", "args": {"STN_NO": {"k": "2"}, "WORD": {"k": "20"}}}}}])
    word = blk("03Msg_Word", "FC", {"Input": [["CFG_MsgWord", "Int"], ["CFG_MsgBit", "Int"]]},
               [{"n": 1, "u": ["SCL"], "scl": "G.Err_Word := CFG_MsgWord;\nG.Err_Bit := CFG_MsgBit;"}])
    explicit = blk("Alarms", "FC", {}, [
        {"n": 1, "calls": {"c1": {"blk": "03Msg_Word", "type": "FC", "args": {"CFG_MsgWord": {"k": "50"}, "CFG_MsgBit": {"k": "0"}}}}},
        {"n": 2, "t": "Air MSG.W[50].x0", "calls": msg("c1", "WaterAir", {"v": "Air.Low", "n": 1}, unit=2)},
        {"n": 3, "t": "Cell door", "calls": msg("c1", "Gate", {"v": "Door.Open"}, unit=4, blkname="01Msg_Cell")},
        {"n": 4, "u": ["SCL"], "scl": "FOR I := 1 TO 16 DO\n  IF Dev.Lost[I] THEN\n    Err := 1;\n  END_IF;\n  01Msg_Cell();\nEND_FOR;",
         "calls": {"c1": {"blk": "01Msg_Cell", "type": "FC", "src": "SCL", "cnd": 1, "loop": {"var": "I", "from": 1, "to": 16, "by": 1}, "txt": "01Msg_Cell()",
                          "args": {"CFG_MsgType": {"v": "Msg_ErrType.Communication"}, "CFG_Cell_No": {"k": "1"}, "MsgSource": {"v": "Err", "l": 1}}}}},
        {"n": 5, "t": "After loop", "calls": msg("c1", "Part", {"v": "Part.Missing"}, unit=2)}])
    main = blk("Main", "OB", {}, [
        {"n": 1, "calls": {"c1": {"blk": "Stns", "type": "FC", "args": {}}}},
        {"n": 2, "calls": {"c1": {"blk": "Alarms", "type": "FC", "args": {}}}}])
    blocks = {b["name"]: b for b in (main, stns, stn, total, obj, explicit, word)}
    return tl.TiaLogic(blocks, "P")


@pytest.fixture(scope="module")
def built():
    return am.build_map(two_stations(), {160: "ST1 UB sensor fault", 164: "Power lost UB", 800: "Air pressure low", 9999: "Lost text"}, "TIA_p_1.zip")


def test_bits_follow_call_order_and_station_start(built):
    a = built["alarms"]
    # станция 1: слово 10; бит +1 за каждый вызов Msg: OBJ1 (2 вызова), OBJ2 (2 вызова), свой вызов станции
    assert [a[str(b)]["blk"] for b in (160, 161, 162, 163, 164)] == ["ObjFb", "ObjFb", "ObjFb", "ObjFb", "StnFb"]
    assert (a["160"]["w"], a["160"]["x"]) == (10, 0) and (a["164"]["w"], a["164"]["x"]) == (10, 4)
    assert [a[str(b)]["unit"] for b in (160, 164, 320, 324)] == [1, 1, 2, 2]       # G.stn_No ведётся с 01Stn_Total
    assert a["320"]["w"] == 20 and a["324"]["blk"] == "StnFb"                      # станция 2: слово 20


def test_type_path_and_instance(built):
    a = built["alarms"]["162"]
    assert a["type"] == "Sonser" and a["net"] == 2
    assert a["path"] == "Main (сеть 1) → Stns (сеть 1) → StnFb [ST1_DB] (сеть 3) → ObjFb [OBJ2] (сеть 2) → 02Msg_Stn"
    assert built["alarms"]["164"]["type"] == "Power" and built["alarms"]["164"]["path"].endswith("StnFb [ST1_DB] (сеть 4) → 02Msg_Stn")


def test_latch_source_and_reset_with_real_signals(built):
    a = built["alarms"]["160"]
    assert a["latched"] is True
    assert a["src"].startswith("ST1_DB.OBJ1.Fault: ") and "IN_A" in a["src"] and "ST1_DB.OBJ1.Timeout" in a["src"]
    assert "Operator.Reset" in a["reset"]
    assert built["alarms"]["320"]["src"].startswith("ST2_DB.OBJ1.Fault") and "IN_B" not in built["alarms"]["320"]["src"]
    assert [w["k"] for w in a["chain"][0]["writes"]] == ["set", "reset"]


def test_plain_source_is_not_latch(built):
    b = built["alarms"]["161"]
    assert b["src"] == "НЕ Gate.Open" and b["latched"] is False and "reset" not in b and "chain" not in b


def test_explicit_word_title_address_and_cell(built):
    a = built["alarms"]
    assert (a["800"]["w"], a["800"]["x"], a["800"]["type"], a["800"]["unit"]) == (50, 0, "WaterAir", 2)
    assert a["800"]["by"] == "text" and a["800"]["by_addr"] is True               # адрес MSG.W[50].x0 в подписи сети совпал
    assert a["801"]["kind"] == "cell" and a["801"]["unit"] == 4


def test_loop_expands_each_step_and_rolls_over_word(built):
    a = built["alarms"]
    assert [a[str(b)]["loop_i"] for b in (802, 817)] == [1, 16] and a["802"]["src"] == "Err := Dev.Lost[1]" and a["817"]["src"] == "Err := Dev.Lost[16]"
    assert a["818"]["w"] == 51 and a["818"]["x"] == 2 and a["818"]["net"] == 5      # 16 шагов цикла + вызов до него заняли слово 50 и бит 2 слова 51


def test_confirmation_by_text_order_none(built):
    a = built["alarms"]
    assert a["160"]["by"] == "order" and a["160"]["flags"] == ["текст аварии не совпал с подписью сети"]    # текст не из этой сети: не подтверждён
    assert a["9999"]["by"] == "none" and a["9999"]["flags"] == ["источник не найден"] and a["9999"]["text"] == "Lost text"
    s = built["stats"]
    assert s["catalog_with_text"] == 4 and s["text_unmapped"] == 1 and s["text_confirmed"] == 1 and s["text_mapped"] == 3


def test_counter_changed_by_condition_marks_bits_unreliable():
    lg = two_stations()
    lg.blocks["Stns"]["nets"].insert(1, {"n": 9, "u": ["SCL"], "scl": "IF G.Cyc THEN\n  G.Err_Bit := 3;\nEND_IF;"})
    d = am.build_map(lg)
    assert any("ненадёжен" in f for f in d["alarms"]["160"].get("flags", []) + d["alarms"]["324"].get("flags", []))


def test_conditional_call_is_flagged_not_hidden():
    lg = two_stations()
    lg.blocks["ObjFb"]["nets"][2]["calls"]["c1"]["cnd"] = 1
    d = am.build_map(lg)
    assert "вызов в условии" in d["alarms"]["161"]["flags"]


def test_no_main_is_clear_error():
    with pytest.raises(ValueError, match="Main"):
        am.build_map(tl.TiaLogic({}, "P"))


def test_constant_comparisons_fold_away():
    node = {"and": [{"cmp": ">=", "a": {"k": "2"}, "b": {"k": "1"}}, {"v": "X"}]}
    assert tl.to_text(am.fold(node)) == "X"
    assert tl.to_text(am.fold({"and": [{"cmp": "==", "a": {"k": "1"}, "b": {"k": "2"}}, {"v": "X"}]})) == "FALSE"


# ---------- настоящая выгрузка GWM_UB ----------
@pytest.fixture(scope="module")
def gwm():
    if not GWM_ZIP.is_file():
        pytest.skip("нет выгрузки TIA_gwm_ub_2026-10-20_13-49.zip")
    catalog = {}
    for line in CATALOG.read_text(encoding="utf-8").splitlines():
        b, t = line.split("\t", 1)
        catalog[int(b)] = t
    return am.build_map(tl.TiaLogic.load(GWM_ZIP), catalog, GWM_ZIP.name)


def test_gwm_clamp_bit_3232_is_mapped_with_condition_and_reset(gwm):
    e = gwm["alarms"]["3232"]
    assert (e["w"], e["x"], e["blk"], e["unit"], e["type"]) == (202, 0, "04stn_obj", 2, "Sonser")
    assert e["path"].endswith("STN_002_UB020 [STN_002_DB] (сеть 13) → 04stn_obj [OBJ5] (сеть 25) → 02Msg_Stn")
    assert e["by"] == "text" and e["latched"] is True
    # ветка «нет сигнала»: ни AWork, ни RHome, 8 секунд, независимо от CloseFault_M
    assert "TON(IEC_Timer_Fwd01" in e["src"] and 'НЕ "UB020_AW001_IN_C1.1-AWork"' in e["src"] and 'НЕ "UB020_AW001_IN_C1.1-RHome"' in e["src"]
    assert "STN_002_DB.OBJ5.CloseFault_M" in e["src"] and "Err_Reset" in e["reset"]


def test_gwm_share_of_mapped_alarms_is_high(gwm):
    s = gwm["stats"]
    assert s["catalog_with_text"] == 1542
    assert s["text_mapped"] >= 1500 and s["text_confirmed"] >= 1350 and s["text_unmapped"] <= 40      # на 22.10: 1528 / 1405 / 14
    assert s["tainted"] == 0
    assert len({e["b"] for e in gwm["alarms"].values()}) == len(gwm["alarms"])


def test_gwm_other_clamps_drivers_and_estops(gwm):
    a = gwm["alarms"]
    for b, station in ((3904, "UB040"), (3952, "UB040")):                          # зажимы UB040: пары AWork/RHome того же зажима
        assert a[str(b)]["unit_name"] == station and a[str(b)]["by"] == "text"
    mb = [e for e in a.values() if e.get("unit_name") == "MB100" and e["blk"] == "04stn_obj" and e.get("text")]
    assert len(mb) == 18 and all(e["by"] == "text" for e in mb)
    drv = a["3218"]
    assert drv["type"] == "Driver" and drv["blk"] == "011Stn_obj_MC07B_lift" and "Inverter Ready" in drv["src"] and drv["by"] == "text"
    es = a["1408"]
    assert es["type"] == "Estop" and es["blk"] == "06CFG_OP_1" and "F.OP_1.ESTOP_FQ" in es["src"] and es["by"] == "text"


def test_gwm_profinet_loop_bits(gwm):
    e = gwm["alarms"]["4"]
    assert e["blk"] == "08Msg_PN_Diagnosis" and e["loop_i"] == 5 and e["src"].startswith("Err := Msg_PN.PN_Loss_Disable[5]") and e["by"] == "text"
