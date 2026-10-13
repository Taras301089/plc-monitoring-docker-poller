"""Экран «FL RB294»: столбцы «Рем. зона» и «Простой BDC», значение STR (прямой сход). Единые подписи и расчёты для Andon, архива и Excel.

Источники (ПЛК AE_WBS_2, индексация массивов TIA с нуля):
- «Рем. зона»: FB_AMMKZ_Andon_DB.RepairCount[k] (Int), счётчик кузовов, ушедших в ремонт; [0] — общий итог за смену, [k] — интервал k;
- «Простой BDC»: DB55_temporary_data_storage.DownTimer_BDC[k].ET (Int32, миллисекунды); [0] не интервал, [k] — интервал k;
- итог дня STR: FB_AMMKZ_Andon_DB.RepairPercent (Float, %), ПЛК считает один раз в конце дня (до этого 0); поллер сохраняет его в kpi_hourly.repair_pct_plc.
Соответствие «интервал → элемент массива» задано в одном месте: element_of_interval().
Поллер хранит последнее значение интервала в kpi_hourly.repair_cnt / bdc_ms; минуты из миллисекунд считают функции столбца «Простой RB262»."""
from __future__ import annotations

import json
import logging
import math
from typing import Any

import asyncpg

from starve import starve_minutes, starve_mmss

REPAIR_SCREEN = "FL RB294"
REPAIR_TITLE = "Рем. зона"
REPAIR_HINT = "Счётчик кузовов. Выход в ремонт"
BDC_TITLE = "Простой BDC"
BDC_HINT = "Подсчитывается автоматически. Время простоя BDC (DownTimer_BDC)"
STR_TITLE = "STR"
STR_HINT = ("STR: прямой сход, отношение кузовов без дефектов к общему количеству кузовов. В течение дня рассчитывается автоматически "
            "(факт минус выход в ремонт, делённое на факт), итог дня берётся из ПЛК (RepairPercent, считается в конце дня)")
REPAIR_DB, REPAIR_ARRAY = "FB_AMMKZ_Andon_DB", "RepairCount"
BDC_DB, BDC_ARRAY = "DB55_temporary_data_storage", "DownTimer_BDC"
INTERVALS = 13                  # интервалов на экране
REPAIR_KEY = "rc_"              # привязка интервала k: bindings["rc_<k>"] = узел RepairCount[element_of_interval(k)]
RPCT_DB, RPCT_FIELD = "FB_AMMKZ_Andon_DB", "RepairPercent"
RPCT_KEY = "rpct"               # привязка итога дня STR из ПЛК: bindings["rpct"] = FB_AMMKZ_Andon_DB.RepairPercent (Float, %; ПЛК считает раз в конце дня, до этого 0)
RPCT_WARN_PP = 0.05             # расхождение расчёта и итога ПЛК (п.п.), с которого пишется предупреждение в журнал
BDC_KEY = "bd_"                 # привязка интервала k: bindings["bd_<k>"] = узел DownTimer_BDC[element_of_interval(k)].ET


def element_of_interval(k: int) -> int:
    """Номер элемента массивов ПЛК для интервала k (интервалы с единицы): интервал 1 = [1], интервал 2 = [2] ... Элемент [0] не интервал."""
    return k


def repair_node_id(k: int) -> str:
    return f'ns=3;s="{REPAIR_DB}"."{REPAIR_ARRAY}"[{element_of_interval(k)}]'


def bdc_node_id(k: int) -> str:
    return f'ns=3;s="{BDC_DB}"."{BDC_ARRAY}"[{element_of_interval(k)}]."ET"'


def rpct_node_id() -> str:
    return f'ns=3;s="{RPCT_DB}"."{RPCT_FIELD}"'


def bindings_for() -> dict[str, str]:
    """Привязки обоих столбцов для интервалов 1 ... INTERVALS и итога дня STR из ПЛК."""
    out: dict[str, str] = {RPCT_KEY: rpct_node_id()}
    for k in range(1, INTERVALS + 1):
        out[f"{REPAIR_KEY}{k}"] = repair_node_id(k)
        out[f"{BDC_KEY}{k}"] = bdc_node_id(k)
    return out


def has_repair(bindings: Any) -> bool:
    """Есть ли у экрана привязка столбцов «Рем. зона» и «Простой BDC»."""
    return isinstance(bindings, dict) and bool(bindings.get(f"{REPAIR_KEY}1"))


def repair_value(v: Any) -> int | None:
    """Кузовов в ремонте за интервал; нет значения -> None."""
    if v is None or isinstance(v, bool):
        return None
    return max(0, int(v))


def repair_total(values: list[Any]) -> int | None:
    """Итог за смену: сумма по интервалам; ни одного значения -> None."""
    got = [int(v) for v in values if v is not None and not isinstance(v, bool)]
    return sum(got) if got else None


bdc_minutes = starve_minutes     # мс -> минуты (две цифры), единая функция столбцов таймеров
bdc_mmss = starve_mmss           # мс -> «М:СС»


def str_percent(fact: Any, repair: Any) -> float | None:
    """STR, %: (факт - ремонт) / факт * 100 с двумя знаками; факт 0 или нет значения -> None («—»)."""
    if fact is None or repair is None or isinstance(fact, bool) or isinstance(repair, bool) or fact <= 0:
        return None
    # округление «от половины вверх» на целых числах (в JS экрана та же арифметика), без расхождений float на границе .005
    num = (int(fact) - int(repair)) * 10000
    cents = (2 * abs(num) + int(fact)) // (2 * int(fact))
    return (cents if num >= 0 else -cents) / 100


def str_label(p: float | None) -> str:
    """Подпись «95,00 %»; нет значения -> «—»."""
    return "—" if p is None else f"{p:.2f}".replace(".", ",") + " %"


def plc_str_value(v: Any) -> float | None:
    """Итог дня STR из ПЛК (RepairPercent, %): число больше 0 и не больше 100, две цифры; 0 (ПЛК ещё не считал), ошибка чтения, не число -> None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 or v > 100:
        return None
    return round(float(v), 2)


def pick_str(fact: Any, repair: Any, plc: Any) -> tuple[float | None, str]:
    """Единственное место выбора STR: итог дня из ПЛК, если он есть (больше 0), иначе расчёт по факту и «Рем. зоне».
    Возвращает (значение, 'plc' или 'calc'). Экран (через API), архив, Excel и автосохранение берут значение отсюда."""
    p = plc_str_value(plc)
    if p is not None:
        return p, "plc"
    return str_percent(fact, repair), "calc"


def plc_str_of(summary: dict[str, Any] | None) -> float | None:
    """Итог ПЛК из str_summary для Excel: значение, если STR взят из ПЛК (в ячейку идёт число), иначе None (в ячейке формула)."""
    return summary["pct"] if summary and summary.get("source") == "plc" else None


_WARNED: set[Any] = set()
log = logging.getLogger(__name__)


def str_summary(rows: list[Any], key: Any = None) -> dict[str, Any]:
    """Итоги дня по строкам kpi_hourly (plan, fact, repair_cnt, repair_pct_plc): факт, кузовов в ремонте и STR.
    Берутся те интервалы, что показаны на экране и в файле (с планом или фактом), чтобы число совпадало с «Итого».
    Итог ПЛК (колонка repair_pct_plc, одно значение на день) берётся по всем строкам дня; при расхождении с расчётом более 0,05 п.п.
    один раз на ключ (экран, день) пишется предупреждение в журнал."""
    plc = None
    for r in rows:
        try:
            p = plc_str_value(r["repair_pct_plc"])
        except (KeyError, IndexError):
            p = None
        if p is not None:
            plc = p
    shown = [r for r in rows if (r["plan"] or 0) > 0 or (r["fact"] or 0) > 0]
    fact = sum(int(r["fact"] or 0) for r in shown)
    rep = repair_total([r["repair_cnt"] for r in shown])
    pct, source = pick_str(fact, rep, plc)
    calc = str_percent(fact, rep)
    if source == "plc" and calc is not None and abs(calc - pct) > RPCT_WARN_PP and key is not None and key not in _WARNED:
        _WARNED.add(key)
        log.warning("STR %s: итог ПЛК %.2f %% расходится с расчётом %.2f %% (факт %s, рем. зона %s); показан итог ПЛК", key, pct, calc, fact, rep)
    return {"fact": fact, "repair": rep, "pct": pct, "source": source, "plc": plc}


async def ensure_repair_binding(pool: asyncpg.Pool) -> int:
    """Экрану «FL RB294» (шаблон Finish Line) добавляет привязки «Рем. зона» и «Простой BDC».
    Узлов этих массивов в обзоре переменных ПЛК может не быть, поэтому условие только экран и шаблон.
    Недостающие ключи (например, rpct у экрана, где «Рем. зона» уже привязана) добавляются, существующие не меняются.
    Возвращает число обновлённых экранов; полностью привязанные и чужие экраны не трогает."""
    done = 0
    rows = await pool.fetch("SELECT id, bindings FROM kpi_screens WHERE name = $1 AND template = 'fl_counter'", REPAIR_SCREEN)
    for r in rows:
        raw = r["bindings"]
        b = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        missing = {k: v for k, v in bindings_for().items() if not b.get(k)}
        if not missing:
            continue
        b.update(missing)
        await pool.execute("UPDATE kpi_screens SET bindings = $2::jsonb WHERE id = $1", r["id"], json.dumps(b))
        done += 1
    return done
