"""Вкладка «Переменные ПЛК»: массовые флажки (выбрать все, протяжка, Shift-диапазон) и подписи столбцов, статические проверки."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
HEAD = HTML[HTML.index('<div class="var-head">'):HTML.index('<ul class="tree" id="rows">')]


def test_head_has_words_instead_of_letters():
    for word in ("Архив", "Grafana", "Оповещение"):
        assert f"<span>{word}</span>" in HEAD
    assert "A · G · L" not in HTML
    assert not re.search(r"> [AGL]</label>", HTML)


def test_select_all_in_every_column_head_with_title():
    for col in ("sel", "archive", "grafana", "alarm"):
        m = re.search(rf'<input[^>]*class="head-all"[^>]*data-col="{col}"[^>]*>', HEAD)
        assert m and 'title="' in m.group(0), col
    assert "Выбрать все" in HEAD
    assert "xlgrid.selectAll(" in HTML


def test_fill_handle_everywhere_with_titles():
    assert len(re.findall(r'<i class="fill-h" title="[^"]{30,}"', HTML)) >= 4
    assert "setPointerCapture" in HTML and "pointerdown" in HTML
    assert "xlgrid.dragFill(" in HTML and "requestAnimationFrame" in HTML
    assert ".in-range" in HTML


def test_shift_range():
    assert "e.shiftKey" in HTML and "xlgrid.shiftRange(" in HTML


def test_flag_cells_have_own_titles():
    for t in ("Архив: сохранять значения", "Grafana: показывать", "Оповещение: включить"):
        assert t in HTML


def test_bulk_goes_in_batches_not_per_row():
    assert "slice(i, i + 500)" in HTML and "body: JSON.stringify({ tags })" in HTML


def test_reference_has_no_letters():
    ref = (STATIC / "reference.html").read_text(encoding="utf-8")
    assert "A/G/L" not in ref
