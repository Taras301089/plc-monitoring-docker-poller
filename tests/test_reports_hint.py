"""Подсказка экрана «Отчёты» описывает один файл в день (отдельного месячного файла больше нет)."""
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "reports.js").read_text(encoding="utf-8")


def test_hint_describes_single_file_per_day():
    assert "один файл Excel" in JS and "один лист" in JS
    assert "сохраняется месячный отчёт" not in JS
