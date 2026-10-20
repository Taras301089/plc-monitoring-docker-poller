"""Строка «ИТОГО» на Andon не растёт по высоте: текст отставания лежит в ячейке на три колонки (Простой, Отв., Причина).

Ошибка (телевизор, 19.10.2026): текст «Отставание за день N мин: …» стоял в одной узкой колонке «Причина», переносился на 4–5 строк,
строка «ИТОГО» вытягивала таблицу, и она не помещалась по высоте. База не нужна."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
SOURCES = {name: (STATIC / name).read_text(encoding="utf-8") for name in ("index.html", "manual.js")}
CELL = re.compile(r'<td\b[^>]*class="resp-sum"[^>]*>')


def test_every_footer_resp_cell_spans_three_columns():
    found = 0
    for name, text in SOURCES.items():
        for m in CELL.finditer(text):
            found += 1
            assert 'colspan="3"' in m.group(0), f"{name}: ячейка отставания в строке ИТОГО должна занимать три колонки: {m.group(0)}"
    assert found == 3, "три раскладки Andon: Main Line, Finish Line, подсборки"


def test_no_empty_cells_squeezing_the_resp_cell():
    for name, text in SOURCES.items():
        assert not re.search(r'<td></td>\s*<td></td>\s*<td\b[^>]*class="resp-sum"', text), f"{name}: две пустые ячейки перед ячейкой отставания (она снова узкая)"
