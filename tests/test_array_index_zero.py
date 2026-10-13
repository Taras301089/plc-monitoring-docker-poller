"""Нумерация элементов массива с нуля, как в TIA Portal: DownTimer[0] в ТИА это [0] на экране."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def _label(name: str) -> str:
    m = re.search(r"function arrayElemLabel\(variableName\) \{[^\n]*\}", HTML)
    assert m, "нет функции arrayElemLabel"
    code = m.group(0) + f"; console.log(JSON.stringify(arrayElemLabel({json.dumps(name)})))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True).stdout)


@node
def test_element_zero_is_labelled_zero():
    assert _label("0") == "[0]"
    assert _label("12") == "[12]"


def test_no_plus_one_shift_for_array_labels():
    assert not re.search(r"Number\(c\.variable_name\)\s*\+\s*1", HTML)
    assert "arrayElemLabel(c.variable_name)" in HTML
