"""Расписание интервалов Andon: проверка значений перед записью в ПЛК (минуты от полуночи)."""
from __future__ import annotations

from typing import Any

INTERVAL_COUNT = 13
DAY_MINUTES = 1440


def validate_schedule(start: list[Any], end: list[Any]) -> str | None:
    """Возвращает текст ошибки или None, если расписание можно писать в ПЛК."""
    if len(start) != INTERVAL_COUNT or len(end) != INTERVAL_COUNT:
        return f"Нужно ровно {INTERVAL_COUNT} периодов (начало и конец у каждого)"
    for name, arr in (("начала", start), ("концы", end)):
        for i, v in enumerate(arr, 1):
            if isinstance(v, bool) or not isinstance(v, int):
                return f"Период {i}: {name} должно быть целым числом минут"
    for i in range(INTERVAL_COUNT):
        s, e = start[i], end[i]
        if not 0 <= s < e <= DAY_MINUTES:
            return f"Период {i + 1}: начало должно быть меньше конца, обе границы от 0 до {DAY_MINUTES} минут"
        if i + 1 < INTERVAL_COUNT:
            if start[i + 1] <= s:
                return f"Период {i + 2}: начало должно быть позже начала периода {i + 1}"
            if e > start[i + 1]:
                return f"Период {i + 1}: конец не может быть позже начала периода {i + 2}"
    return None
