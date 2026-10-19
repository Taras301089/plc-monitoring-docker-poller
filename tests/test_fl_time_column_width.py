"""Табло FL: колонка «Время» достаточно широка, чтобы «07:30-08:30» не переносилось на две строки.

Шрифт табло ограничен шириной экрана (1,85vw), тексту «ЧЧ:ММ-ЧЧ:ММ» нужно 5,31 em плюс отступы, то есть чуть больше 10% ширины таблицы.
Если время переносится, каждая строка вдвое выше, 13 строк FL и «ИТОГО» не помещаются на телевизоре (проверено 19.10.2026 на 1280×720)."""
import re
from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def test_fl_andon_time_column_is_wide_enough():
    m = re.search(r'<colgroup><col style="width:3%"><col style="width:([\d.]+)%">(?=<col style="width:5%">.*\$\{FL_MODELS)', INDEX)
    assert m, "не найден colgroup экрана FL"
    assert float(m.group(1)) >= 11.0, "колонка «Время» у FL уже 11%: время перенесётся на две строки, таблица не поместится на табло"
