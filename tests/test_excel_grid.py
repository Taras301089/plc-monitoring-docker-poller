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
