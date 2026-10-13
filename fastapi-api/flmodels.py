"""Столбцы моделей (A01, P01, CS55, A13T) таблицы интервалов экрана Finish Line: штуки по интервалу.
Один источник для Excel и предпросмотра: тот же массив ПЛК (ключи привязки m_<модель>), что читает экран Andon.
В базе по интервалам модели не хранятся, поэтому значения есть только за сегодняшний день; за прошлые дни и при сбое чтения «—»."""
from __future__ import annotations

from datetime import date
from typing import Any

import rest_bodies as rb

FL_TEMPLATE = "fl_counter"
MODEL_KEY = "m_"                                   # привязка модели m: bindings["m_<m>"] = узел массива «штуки по интервалам»
MODELS = list(rb.REST_FIELDS)                      # порядок как у столбцов моделей на экране: A01, P01, CS55, A13T
MODEL_HINT = "Количество кузовов модели {m} за интервал"


def model_hint(model: str) -> str:
    return MODEL_HINT.format(m=model)


def has_models(template: str | None) -> bool:
    """Показывает ли экран столбцы моделей: их рисует только шаблон Finish Line (на экране они всегда, независимо от привязок)."""
    return template == FL_TEMPLATE


def counts_from_values(bindings: dict[str, Any], values: dict[str, Any]) -> dict[str, list[int] | None]:
    """Массивы по моделям из ответа ПЛК {node_id: {ok, v}}; нет значения или не массив -> None."""
    out: dict[str, list[int] | None] = {}
    for m in MODELS:
        t = values.get(bindings.get(f"{MODEL_KEY}{m}", ""))
        v = t.get("v") if isinstance(t, dict) and t.get("ok") else None
        out[m] = [x if isinstance(x, int) and not isinstance(x, bool) else 0 for x in v] if isinstance(v, list) else None
    return out


async def fetch_counts(plc_id: int, bindings: dict[str, Any], day: date) -> dict[str, list[int] | None]:
    """Штуки по моделям для Excel: сегодня читаем ПЛК тем же маршрутом, что экран; прошлый день или сбой связи -> все None («—»)."""
    none: dict[str, list[int] | None] = {m: None for m in MODELS}
    nodes = [bindings.get(f"{MODEL_KEY}{m}") for m in MODELS]
    if not all(nodes) or day != rb.plant_today():
        return none
    try:
        values = await rb.read_poller_values(plc_id, nodes)
    except Exception:
        return none
    return counts_from_values(bindings, values)


def interval_value(counts: dict[str, list[int] | None], model: str, idx: int) -> int | str:
    """Штуки модели в интервале с номером idx (с единицы, как kpi_hourly.idx); нет данных -> «—»."""
    arr = counts.get(model)
    if arr is None:
        return "—"
    return arr[idx - 1] if 0 < idx <= len(arr) else 0
