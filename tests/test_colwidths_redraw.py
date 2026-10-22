"""Ширины колонок при перерисовке таблицы: таблица, у которой заменили содержимое (innerHTML), измеряется заново
в обычной раскладке. Иначе фиксированная раскладка от прошлого показа сжимает узкие колонки (width: 1%, «Вкл»)
почти до нуля, и соседний заголовок наезжает на них (экран «Оповещения», 20.10.2026)."""
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "colwidths.js").read_text(encoding="utf-8")


def _attach_pixels():
    start = JS.index("function attachPixels")
    return JS[start:JS.index("window.attachColWidths", start)]


def test_previous_fixed_layout_is_reset_before_measuring():
    body = _attach_pixels()
    measure = body.index("getBoundingClientRect")
    assert "table.style.tableLayout = ''" in body[:measure], "прежняя фиксированная раскладка не сброшена до измерения"
    assert "table.style.width = ''" in body[:measure], "прежняя ширина таблицы не сброшена до измерения"
