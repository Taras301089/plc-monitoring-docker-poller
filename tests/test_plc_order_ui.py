"""Порядок ПЛК в списке (index.html): кнопок ∧ ∨ больше нет, порядок личный и меняется перетаскиванием (reorder.js)."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def test_move_buttons_removed_from_plc_list():
    assert "plc-up" not in HTML and "plc-down" not in HTML
    assert "iconUp" not in HTML and "iconDown" not in HTML
    assert "async function movePlc" not in HTML


def test_plc_list_is_draggable_with_personal_order():
    assert "attach(plcList" in HTML and "key: 'order-plc'" in HTML
    assert "sortPlcs(" in HTML                       # личный порядок применяется при загрузке и при опросе статуса


def test_edit_and_delete_buttons_do_not_start_drag():
    assert "ignoreSelector: '.plc-icon-btn'" in HTML
