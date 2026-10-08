"""Андон: клик по «не описан» в колонке «Причина» открывает редактор простоя этой строки (статическая проверка)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "downtime.js").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")


def test_not_described_is_marked_clickable_with_title():
    m = re.search(r'<span class="why-none why-none-click" title="([^"]{40,})">не описан</span>', JS)
    assert m, "у «не описан» нет класса why-none-click и title"


def test_click_handler_opens_same_panel_as_minutes_button():
    m = re.search(r"whyTd\.onclick = e => \{(.*?)\n      \};", JS, re.S)
    assert m, "нет обработчика клика колонки «Причина»"
    body = m.group(1)
    assert "why-none-click" in body
    assert "openPanel(screen, d.idx, tr)" in body
    # та же функция, что у кнопки с минутами
    assert "addEventListener('click', () => openPanel(screen, d.idx, tr))" in JS


def test_css_pointer_and_variables_only():
    assert re.search(r"why-none-click\s*\{[^}]*cursor:\s*pointer", CSS)
    block = "\n".join(re.findall(r"[^\n]*why-none-click[^\n]*", CSS))
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", block)
