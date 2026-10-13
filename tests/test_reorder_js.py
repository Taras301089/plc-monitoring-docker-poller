"""Перетаскивание порядка (reorder.js): чистые функции в node и подключение на экранах."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = STATIC / "reorder.js"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
ALARMS = (STATIC / "alarms.js").read_text(encoding="utf-8")
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def run(expr: str):
    code = f"const r = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@node
def test_apply_order_unknown_to_end_and_missing_ignored():
    assert run("r.applyOrder(['a','b','c','d'], ['c','x','a'], x => x)") == ["c", "a", "b", "d"]
    assert run("r.applyOrder(['a','b'], [], x => x)") == ["a", "b"]
    assert run("r.applyOrder(['a','b'], null, x => x)") == ["a", "b"]
    assert run("r.applyOrder([{id:1},{id:2},{id:3}], ['3','1'], x => x.id).map(x => x.id)") == [3, 1, 2]


@node
def test_apply_order_is_stable_for_new_items():
    assert run("r.applyOrder(['n1','a','n2','b'], ['b','a'], x => x)") == ["b", "a", "n1", "n2"]


@node
def test_move_item():
    assert run("r.moveItem(['a','b','c'], 'a', 'c')") == ["b", "a", "c"]
    assert run("r.moveItem(['a','b','c'], 'c', 'a')") == ["c", "a", "b"]
    assert run("r.moveItem(['a','b','c'], 'a', null)") == ["b", "c", "a"]
    assert run("r.moveItem(['a','b','c'], 'zz', 'a')") == ["a", "b", "c"]


def test_script_connected_before_main_script():
    assert '<script src="/static/reorder.js"></script>' in HTML
    assert HTML.index("/static/reorder.js") < HTML.index("/static/alarms.js")


def test_arrows_removed_from_plc_list():
    assert "plc-up" not in HTML and "plc-down" not in HTML


def test_attach_reorder_used_on_all_screens():
    assert "key: 'order-tabs'" in HTML                      # вкладки верхнего меню
    assert "key: 'order-andon-brands'" in HTML              # бренды Andon
    assert "'order-andon-lines-'" in HTML                   # линии внутри бренда
    assert "key: 'order-plc'" in HTML                       # список ПЛК
    assert "'order-alarms-brands'" in ALARMS                # бренды вкладки «Аварии»
    assert "'order-plc'" in ALARMS                          # кнопки ПЛК на экране «Аварии» идут в том же порядке


def test_right_group_of_tabs_is_not_draggable():
    assert "itemSelector: ':scope > .tab[data-tab]'" in HTML   # правая группа лежит во вложенном блоке и не входит


def test_titles_and_cursor():
    js = JS.read_text(encoding="utf-8")
    assert "Перетащите, чтобы изменить порядок (порядок хранится у вас на сервере)" in js
    assert "Shift при отпускании" in js
    assert ".reorder-item { cursor: grab; }" in HTML


def test_server_timeout_and_gesture_rules():
    js = JS.read_text(encoding="utf-8")
    assert "AbortSignal.timeout(TIMEOUT)" in js and "TIMEOUT = 6000" in js
    assert "250" in js and "Escape" in js and "pointerdown" in js
