"""Столбец «Простой RB262» (голодание ФЛ): единые подпись и расчёт для Andon, Отчётов, Графиков и Excel.

Источник: накопительные таймеры ПЛК DB_AMMKZ_Andon.DownTimer[i].ET (Int32, миллисекунды), элемент i = интервал i+1.
Поллер сохраняет последнее значение интервала в kpi_hourly.starve_ms; здесь минуты считаются из миллисекунд единственной формулой."""
from __future__ import annotations

import json
from typing import Any

import asyncpg

STARVE_TITLE = "Простой RB262"
STARVE_HINT = "Подсчитывается автоматически. Голодание. Счётчик запускается при отсутствии кузовов в накопителе (буфере) перед ФЛ"
STARVE_DB = "DB_AMMKZ_Andon"
STARVE_ARRAY = "DownTimer"
STARVE_SCREEN = "FL RB262"
STARVE_INTERVALS = 13          # интервалов на экране; ключи привязки dt_0 ... dt_12
STARVE_KEY = "dt_"             # привязка интервала i: bindings["dt_<i>"] = node_id поля ET


def starve_node_id(i: int) -> str:
    """Узел поля ET элемента i массива таймеров (индексы с нуля, как в TIA Portal)."""
    return f'ns=3;s="{STARVE_DB}"."{STARVE_ARRAY}"[{i}]."ET"'


def has_starve(bindings: Any) -> bool:
    """Есть ли у экрана привязка таймеров голодания."""
    return isinstance(bindings, dict) and bool(bindings.get(f"{STARVE_KEY}0"))


def starve_minutes(ms: Any) -> float | None:
    """Минуты из миллисекунд ET (две цифры после запятой); нет значения -> None."""
    if ms is None or isinstance(ms, bool):
        return None
    return round(max(0, int(ms)) / 60000, 2)


def starve_mmss(ms: Any) -> str:
    """Подпись «М:СС» (при часе и больше «Ч:ММ:СС»); нет значения -> «—»."""
    if ms is None or isinstance(ms, bool):
        return "—"
    sec = max(0, int(ms)) // 1000
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def starve_total_ms(values: list[Any]) -> int | None:
    """Итог за смену: сумма по интервалам; ни одного значения -> None."""
    got = [int(v) for v in values if v is not None and not isinstance(v, bool)]
    return sum(got) if got else None


async def ensure_starve_binding(pool: asyncpg.Pool) -> int:
    """Экрану «FL RB262» добавляет привязку таймеров голодания, если ПЛК экрана знает массив DownTimer блока DB_AMMKZ_Andon.
    Возвращает число обновлённых экранов; уже привязанные и чужие экраны не трогает."""
    done = 0
    rows = await pool.fetch(
        "SELECT s.id, s.bindings FROM kpi_screens s WHERE s.name = $1 AND s.template = 'fl_counter' AND EXISTS ("
        "SELECT 1 FROM plc_discovered_nodes n WHERE n.plc_id = s.plc_id AND n.node_id = $2)",
        STARVE_SCREEN, f'ns=3;s="{STARVE_DB}"."{STARVE_ARRAY}"',
    )
    for r in rows:
        raw = r["bindings"]
        b = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if has_starve(b):
            continue
        b.update({f"{STARVE_KEY}{i}": starve_node_id(i) for i in range(STARVE_INTERVALS)})
        await pool.execute("UPDATE kpi_screens SET bindings = $2::jsonb WHERE id = $1", r["id"], json.dumps(b))
        done += 1
    return done
