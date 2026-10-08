"""Цветная точка у ПЛК означает только наличие связи (успешный опрос), а не флаг «Включён в опрос» (is_active)."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def test_dot_is_not_driven_by_is_active():
    assert "is_active ? 'on' : 'off'" not in HTML, "точка не должна зависеть от is_active"
    assert "current.is_active ? 'on' : 'off'" not in HTML
    assert "item.is_active ? 'on' : 'off'" not in HTML


def test_switched_off_state_is_shown_by_grey_row_not_by_dot():
    assert ".plc-dd-row.off .plc-dd-name" in HTML
    assert "(item.is_active ? '' : ' off')" in HTML
