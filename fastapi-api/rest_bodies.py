"""Строка «Остаток кузовов на ФЛ»: текущее число кузовов на фуллайне по моделям (живое значение из ПЛК, не по интервалам).

Источник общий для экранов «FL RB262» и «FL RB294» (ПЛК AE_WBS_2): DB_TotalBodiesList.WBS2_Model_263_294.ModelN (Int16).
Значения хранятся не в базе: ключи привязки экрана rest_<модель> указывают на узлы ПЛК, их читает тот же опрос, что и остальные значения экрана.
Для прошлых дней строки нет: «сейчас» к прошлому дню не относится."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx

REST_TITLE = "Остаток кузовов на ФЛ"
REST_HINT = "Текущее число кузовов на фуллайне (ФЛ) по моделям, значение читается из ПЛК"
REST_DB = "DB_TotalBodiesList"
REST_STRUCT = "WBS2_Model_263_294"
REST_KEY = "rest_"                                   # привязка модели m: bindings["rest_<m>"] = node_id
REST_FIELDS = {"A01": "Model4", "P01": "Model5", "CS55": "Model7", "A13T": "Model1"}   # порядок как у столбцов моделей на экране
REST_SCREENS = ("FL RB262", "FL RB294")
POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")
UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))


def rest_node_id(model: str) -> str:
    """Узел поля счётчика модели в структуре WBS2_Model_263_294."""
    return f'ns=3;s="{REST_DB}"."{REST_STRUCT}"."{REST_FIELDS[model]}"'


def has_rest(bindings: Any) -> bool:
    """Есть ли у экрана привязки остатка кузовов (строка показывается только у таких экранов)."""
    return isinstance(bindings, dict) and all(bindings.get(f"{REST_KEY}{m}") for m in REST_FIELDS)


def rest_from_values(bindings: dict[str, Any], values: dict[str, Any]) -> dict[str, int | None]:
    """Значения по моделям из ответа ПЛК {node_id: {ok, v}}; нет значения или ошибка чтения -> None (на экране и в файле «—»)."""
    out: dict[str, int | None] = {}
    for m in REST_FIELDS:
        t = values.get(bindings.get(f"{REST_KEY}{m}", ""))
        v = t.get("v") if isinstance(t, dict) and t.get("ok") else None
        out[m] = v if isinstance(v, int) and not isinstance(v, bool) else None
    return out


def plant_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).date()


async def read_poller_values(plc_id: int, node_ids: list[str]) -> dict[str, Any]:
    """Чтение узлов через поллер (тот же маршрут, что у экрана Andon); при сбое исключение."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
        r = await client.post(f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/values", json={"node_ids": node_ids})
    r.raise_for_status()
    return r.json().get("values", {})


async def fetch_rest(plc_id: int, bindings: dict[str, Any], day: date) -> dict[str, int | None] | None:
    """Остаток для Excel. None = строки нет (экран без привязок или не сегодняшний день);
    при недоступном ПЛК все значения None («—»): устаревших цифр не показываем."""
    if not has_rest(bindings) or day != plant_today():
        return None
    try:
        values = await read_poller_values(plc_id, [bindings[f"{REST_KEY}{m}"] for m in REST_FIELDS])
    except Exception:
        values = {}
    return rest_from_values(bindings, values)


async def ensure_rest_bindings(pool: asyncpg.Pool) -> int:
    """Экранам «FL RB262» и «FL RB294» добавляет привязки остатка кузовов, если их ПЛК знает структуру WBS2_Model_263_294.
    Возвращает число обновлённых экранов; уже привязанные и чужие экраны не трогает."""
    done = 0
    parent = f'ns=3;s="{REST_DB}"."{REST_STRUCT}"'
    rows = await pool.fetch(
        "SELECT s.id, s.bindings FROM kpi_screens s WHERE s.name = ANY($1::text[]) AND s.template = 'fl_counter' AND EXISTS ("
        "SELECT 1 FROM plc_discovered_nodes n WHERE n.plc_id = s.plc_id AND left(n.node_id, length($2)) = $2)",
        list(REST_SCREENS), parent,
    )
    for r in rows:
        raw = r["bindings"]
        b = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if has_rest(b):
            continue
        b.update({f"{REST_KEY}{m}": rest_node_id(m) for m in REST_FIELDS})
        await pool.execute("UPDATE kpi_screens SET bindings = $2::jsonb WHERE id = $1", r["id"], json.dumps(b))
        done += 1
    return done
