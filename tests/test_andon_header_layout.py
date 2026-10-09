"""Шапка Andon: плитка линии не должна наезжать на часы. Симметричная раскладка (1fr auto 1fr) годится, только пока часы
вместе с кнопками справа помещаются в правую колонку; в режиме «архив» кнопок больше, поэтому переход на раскладку
«auto 1fr auto» делается при ширине до 1700px, а не до 1400px."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def test_tight_grid_starts_at_1700px_not_1400px():
    m = re.search(r"@media \(max-width: 1700px\) \{\s*\.andon-layout, body\.tv-mode \.andon-layout \{ grid-template-columns: auto minmax\(0, 1fr\) auto; \}", HTML)
    assert m, "до 1700px шапка должна использовать колонки auto / 1fr / auto, чтобы часы не наезжали на плитку линии"


def test_wide_screens_keep_the_symmetric_header():
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in HTML
