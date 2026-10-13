"""Excel-подобная таблица «Запись переменных»: чистая логика xlgrid.js (через node) и статические проверки."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")


def run(expr):
    code = f"const g=require({json.dumps(str(STATIC / 'xlgrid.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return json.loads(out)


def test_tsv_roundtrip_and_comma():
    assert run("g.parseTsv('1,5\\t2\\r\\n\\t3\\r\\n')") == [["1,5", "2"], ["", "3"]]
    assert run("g.toTsv([[1.5,null],[null,3]])") == "1.5\t\n\t3"
    assert run("g.parseTsv(g.toTsv([[1,null,3],[null,5,null]]))") == [["1", "", "3"], ["", "5", ""]]
    assert run("[g.parseNum('1,5'),g.parseNum(' 2.5 '),g.parseNum('')]") == [1.5, 2.5, None]
    assert run("[Number.isNaN(g.parseNum('abc')),Number.isNaN(g.parseNum('1,2,3'))]") == [True, True]


def test_fill_down():
    assert run("g.fillDown([['5']],3)") == [["5"], ["5"], ["5"]]
    assert run("g.fillDown([['1'],['2']],5)") == [["1"], ["2"], ["1"], ["2"], ["1"]]
    assert run("g.fillDown([],3)") == []


def test_paste_clipped_and_clear():
    cells = run("g.pasteCells(g.parseTsv('1\\t2\\t3\\n4\\t5\\t6'),3,1,4,3)")
    assert cells == [{"r": 3, "c": 1, "v": "1"}, {"r": 3, "c": 2, "v": "2"}]
    assert len(run("g.pasteCells(g.parseTsv('1\\t2\\t3\\n4\\t5\\t6'),0,0,5,3)")) == 6
    assert run("g.clearCells(1,2,0,1)") == [{"r": r, "c": c, "v": ""} for r in (1, 2) for c in (0, 1)]


def test_changed_compare():
    assert run("[g.sameValue('1,5',1.5),g.sameValue('',null),g.sameValue('',0),g.sameValue('2',null),g.sameValue('x',1)]") == [True, True, False, False, False]


def test_validation():
    assert run("g.validateRow({deadband:'1',limit_low:'207',limit_high:'253'})") == {}
    assert run("g.validateRow({deadband:'',limit_low:'',limit_high:''})") == {}
    assert "deadband" in run("g.validateRow({deadband:'-1',limit_low:'',limit_high:''})")
    e = run("g.validateRow({deadband:'',limit_low:'253',limit_high:'207'})")
    assert "limit_low" in e and "limit_high" in e
    assert "limit_low" in run("g.validateRow({deadband:'',limit_low:'253',limit_high:'253'})")
    assert "limit_high" in run("g.validateRow({deadband:'',limit_low:'',limit_high:'abc'})")


def test_static_wiring():
    js = (STATIC / "trends.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    grid = (STATIC / "xlgrid.js").read_text(encoding="utf-8")
    assert "/static/xlgrid.js" in html and html.index("xlgrid.js") < html.index("trends.js")
    assert "attachColWidths(box.querySelector('table'), 'trends-rec')" in js
    assert 'class="tr-save"' not in js and ">Сохранить</button>" not in js
    assert "Сохранить изменения</button>" in js and "Отменить изменения</button>" in js
    assert "xg-handle" in js
    for bid in ("tr-rec-save", "tr-rec-undo"):
        tag = re.search(rf'<button[^>]*id="{bid}"[^>]*>', js).group(0)
        assert 'title="' in tag, bid
    for tag in re.findall(r"<(?:button|input)\b[^>]*>", js[js.index("function drawTags"):js.index("function updateButtons")]):
        assert "title=" in tag, tag
    assert "module.exports" in grid and "root.xlgrid" in grid


# ===== Флажки переменных: массовые операции =====
def _st(*flags):
    return "[" + ",".join("{sel:%s,archive:%s,grafana:false,alarm:false}" % (("true" if f[0] else "false"), ("true" if f[1] else "false")) for f in flags) + "]"


def test_set_flag_coupling():
    assert run("g.setFlag({sel:false},'archive',true)")["sel"] is True
    r = run("g.setFlag({sel:true,archive:true,grafana:true,alarm:true},'sel',false)")
    assert r == {"sel": False, "archive": False, "grafana": False, "alarm": False}
    assert run("g.setFlag({sel:true,archive:true},'archive',false)")["sel"] is True


def test_range_between():
    assert run("g.rangeBetween(5,2,10)") == {"from": 2, "to": 5}
    assert run("g.rangeBetween(2,2,10)") == {"from": 2, "to": 2}
    assert run("g.rangeBetween(-1,2,10)") is None
    assert run("g.rangeBetween(2,10,10)") is None


def test_drag_fill_takes_source_state_both_directions():
    states = _st((True, False), (False, False), (False, False), (False, False))
    r = run(f"g.dragFill({states},0,2,'sel')")
    assert [s["sel"] for s in r["states"]] == [True, True, True, False] and r["changed"] == [1, 2]
    states = _st((False, False), (False, False), (True, False), (True, False))
    r = run(f"g.dragFill({states},2,0,'sel')")
    assert [s["sel"] for s in r["states"]] == [True, True, True, True] and r["changed"] == [0, 1]
    # источник снят: протяжка снимает выбор и флаги у протянутых строк
    states = "[{sel:false,archive:false},{sel:true,archive:true},{sel:true,archive:true}]"
    r = run(f"g.dragFill({states},0,2,'sel')")
    assert [(s["sel"], s["archive"]) for s in r["states"]] == [(False, False)] * 3 and r["changed"] == [1, 2]


def test_shift_range_sets_clicked_value_between_anchor_and_target():
    states = _st(*[(False, False)] * 6)
    r = run(f"g.shiftRange({states},1,4,'archive',true)")
    assert [s["archive"] for s in r["states"]] == [False, True, True, True, True, False]
    assert all(r["states"][i]["sel"] for i in (1, 2, 3, 4)) and r["changed"] == [1, 2, 3, 4]
    assert run(f"g.shiftRange({states},4,1,'archive',true)")["changed"] == [1, 2, 3, 4]
    assert run(f"g.shiftRange({states},-1,1,'archive',true)")["changed"] == []


def test_select_all_toggles_and_counts():
    states = _st((True, False), (False, False), (False, False))
    r = run(f"g.selectAll({states},'sel')")
    assert r["value"] is True and r["changed"] == [1, 2]
    states = _st((True, False), (True, False))
    r = run(f"g.selectAll({states},'sel')")
    assert r["value"] is False and r["changed"] == [0, 1]
    assert run("g.selectAll([],'sel').changed") == []
    assert run(f"g.columnSummary({_st((True, False), (False, False))},'sel')") == {"total": 2, "checked": 1, "all": False, "none": False}


def test_select_all_thousands_fast():
    out = run("(()=>{const s=Array.from({length:20000},()=>({sel:false}));const r=g.selectAll(s,'archive');return [r.changed.length,r.states[19999].sel]})()")
    assert out == [20000, True]


def test_rows_word():
    assert [run(f"g.rowsWord({n})") for n in (1, 2, 5, 11, 21, 112)] == ["1 строка", "2 строки", "5 строк", "11 строк", "21 строка", "112 строк"]
