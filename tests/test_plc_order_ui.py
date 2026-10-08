"""Кнопки «выше»/«ниже» в списке ПЛК (index.html): есть у администратора, вызывают API и не перезагружают переменные."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def test_move_buttons_have_titles_and_edge_disabling():
    assert 'class="plc-icon-btn plc-up"' in HTML and 'class="plc-icon-btn plc-down"' in HTML
    assert "idx === 0 ? ' disabled'" in HTML and "idx === plcItems.length - 1 ? ' disabled'" in HTML
    for cls in ("plc-up", "plc-down"):
        i = HTML.index(f'class="plc-icon-btn {cls}"')
        assert "title=" in HTML[i:i + 400]


def test_move_buttons_only_for_admin_and_call_api():
    i = HTML.index("${admin ? `<button type=\"button\" class=\"plc-icon-btn plc-up\"")
    assert i > 0                                   # кнопки внутри ветки «только администратор»
    assert "/move`" in HTML and "direction" in HTML


def test_move_does_not_reload_variables():
    body = HTML[HTML.index("async function movePlc"): HTML.index("async function deletePlc")]
    assert "loadVariables" not in body and "loadPlcs" not in body
