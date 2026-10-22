"""Андон: клик по тексту описания причины открывает окно комментария, клик по минутам открывает описание простоя."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "downtime.js").read_text(encoding="utf-8")
CM = (STATIC / "comments.js").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")

node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def _helpers(expr: str):
    code = (
        "global.window={addEventListener(){}};global.document={body:{classList:{contains(){return false}}}};"
        "global.plantClock={fmt:x=>x};"
        + "(function(){" + JS + "})();"
        + "const h=window.dtCmHelpers;console.log(JSON.stringify(" + expr + "));"
    )
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout   # код через стандартный ввод: в командной строке Windows не больше 32 767 знаков
    return json.loads(out)


@node
def test_title_has_interval_and_time_without_line_name():
    assert _helpers("h.cmTitle(3,'09:00-10:00')") == "Комментарий — интервал 3, 09:00–10:00"
    assert _helpers("h.cmTitle(5,'')") == "Комментарий — интервал 5"
    assert _helpers("h.cmTitle(2,'08:30-09:30','ОТО')") == "Ответ отдела ОТО и комментарии — интервал 2, 08:30–09:30"   # единое окно ОТО/ИТО
    assert _helpers("h.cmTitle(null,'20.10 08:30–09:30','ИТО')") == "Ответ отдела ИТО и комментарии — 08:30–09:30"      # из списка «Простои» номера интервала нет


@node
def test_access_tv_viewer_and_writer():
    assert _helpers("h.cmAccess(true,null)") == {"open": False, "write": False}
    assert _helpers("h.cmAccess(false,null)") == {"open": True, "write": False}
    assert _helpers("h.cmAccess(false,{role:'viewer'})") == {"open": True, "write": False}
    assert _helpers("h.cmAccess(true,{role:'master'})") == {"open": True, "write": True}


@node
def test_place_reason_area_station():
    d = "{areas:[{id:1,name:'Main Line',stations:[{id:7,name:'UB-20'}]}],reasons:[{id:2,name:'Оборудование'}]}"
    assert _helpers(f"h.cmPlace({d},{{area_id:1,station_id:7,reason_id:2}})") == "Оборудование · Main Line · UB-20"
    assert _helpers(f"h.cmPlace({d},{{reason_id:2}})") == "Оборудование"


def test_description_click_opens_comment_window_minutes_open_panel():
    m = re.search(r"whyTd\.onclick = e => \{(.*?)\n      \};", JS, re.S)
    assert m
    body = m.group(1)
    assert "openCommentPanel(screen, d.idx, tr, Number(col.dataset.item))" in body
    assert "why-none-click" in body and "openPanel(screen, d.idx, tr)" in body
    # кнопка с минутами по-прежнему открывает окно описания
    assert "addEventListener('click', () => openPanel(screen, d.idx, tr))" in JS


def test_clickable_column_has_item_and_title():
    assert re.search(r'why-col\$\{x\.id \? \' why-click\' : \'\'\}', JS)
    assert "Нажмите, чтобы добавить комментарий к этому простою" in JS


def test_window_buttons_keys_and_existing_api():
    assert "window.dtComments.mount(box, it.id" in JS and "open: true" in JS
    assert ">Сохранить</button>" in JS and "'Отмена' : 'Закрыть'" in JS
    assert "e.key === 'Escape'" in JS and "e.ctrlKey" in JS
    assert "opts.open" in CM
    # сервер и новый API не нужны: используется существующий /items/{id}/comments
    assert "/api/kpi/items/${itemId}/comments" in CM


def test_css_pointer_not_on_tv_without_login_and_variables_only():
    assert re.search(r"why-click\s*\{\s*cursor:\s*default", CSS)
    assert re.search(r"body:not\(\.tv-mode\) \.kpi-table td\.why \.why-click[^{]*\{\s*cursor:\s*pointer", CSS)
    block = CSS[CSS.index("Клик по тексту описания"):]
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", block)
