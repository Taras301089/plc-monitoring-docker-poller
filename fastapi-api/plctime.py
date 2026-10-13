"""Время ПЛК и расхождение с сервером PMM (только чтение).

Время ПЛК читается стандартным узлом OPC UA «текущее время сервера» (i=2258, ns=0) через поллер, тем же маршрутом, что и остальные значения.
PMM время ПЛК не выставляет и в ПЛК ничего не пишет: синхронизацию делает сам ПЛК по NTP (настраивается в TIA Portal).
Расхождение = время ПЛК минус время сервера в середине запроса (учитывает задержку чтения), плюс означает «ПЛК впереди».
Результат хранится в памяти процесса (таблица в БД не нужна): последнее значение каждого ПЛК и момент чтения."""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

NODE_ID = "i=2258"
POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")
READ_TIMEOUT_SEC = 5.0          # тайм-аут чтения одного ПЛК; зависший ПЛК не задерживает остальных (чтение идёт параллельно)
CACHE_TTL_SEC = 55.0            # не чаще раза в минуту на ПЛК, сколько бы экранов ни спрашивало
DRIFT_LIMIT_SEC = 5             # расхождение больше этого значения на экране красное

CACHE: dict[int, dict[str, Any]] = {}      # plc_id -> {"mono": момент чтения (monotonic), "result": ответ API}
_lock = asyncio.Lock()


def compute_drift(plc_ts: float, sent_ts: float, received_ts: float) -> int:
    """Расхождение в целых секундах: время ПЛК минус время сервера в середине запроса."""
    return math.floor(plc_ts - (sent_ts + received_ts) / 2 + 0.5)


def parse_plc_time(value: Any) -> float | None:
    """ISO-строка времени от поллера -> unix-секунды; без зоны считается UTC (так отдаёт OPC UA); непонятное значение -> None."""
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def fmt_drift(sec: int | None) -> str:
    """«+5м 28с», «-1м 47с», «+24с», «0с»; нет значения -> «—». Тот же текст строит plctime.js."""
    if sec is None:
        return "—"
    s = int(sec)
    if s == 0:
        return "0с"
    minutes, rest = divmod(abs(s), 60)
    sign = "+" if s > 0 else "-"
    return f"{sign}{minutes}м {rest}с" if minutes else f"{sign}{rest}с"


def is_drifted(sec: int | None) -> bool:
    return sec is not None and abs(sec) > DRIFT_LIMIT_SEC


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def read_values(plc_id: int, node_ids: list[str]) -> dict[str, Any]:
    """Чтение узлов через поллер (тот же маршрут, что у экрана Andon); при сбое исключение."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT_SEC, connect=3.0)) as client:
        r = await client.post(f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/values", json={"node_ids": node_ids})
    r.raise_for_status()
    return r.json().get("values", {})


def _failed(plc_id: int, at: float) -> dict[str, Any]:
    return {"plc_id": plc_id, "plc_time": None, "drift_sec": None, "read_at": _iso(at), "ok": False}


async def read_one(plc_id: int) -> dict[str, Any]:
    """Прочитать время одного ПЛК. Любой сбой (нет связи, тайм-аут, плохое значение) даёт ok=false, без цифр."""
    sent = time.time()
    try:
        values = await asyncio.wait_for(read_values(plc_id, [NODE_ID]), timeout=READ_TIMEOUT_SEC)
    except Exception:
        return _failed(plc_id, time.time())
    received = time.time()
    node = values.get(NODE_ID) if isinstance(values, dict) else None
    plc_ts = parse_plc_time(node.get("v")) if isinstance(node, dict) and node.get("ok") else None
    if plc_ts is None:
        return _failed(plc_id, received)
    return {
        "plc_id": plc_id,
        "plc_time": _iso(plc_ts),
        "drift_sec": compute_drift(plc_ts, sent, received),
        "read_at": _iso((sent + received) / 2),            # момент, которому соответствует plc_time
        "ok": True,
    }


async def read_many(plc_ids: list[int]) -> list[dict[str, Any]]:
    """Параллельное чтение: ПЛК без ответа ждут свой тайм-аут одновременно, а не по очереди."""
    return list(await asyncio.gather(*(read_one(i) for i in plc_ids)))


async def collect(pool: Any) -> list[dict[str, Any]]:
    """Ответ GET /api/plcs/time: по каждому ПЛК время, расхождение и признак ok; значения старше минуты перечитываются."""
    rows = await pool.fetch("SELECT id, is_active FROM plcs ORDER BY sort_order NULLS LAST, id")
    async with _lock:
        now = time.monotonic()
        for gone in set(CACHE) - {r["id"] for r in rows}:
            CACHE.pop(gone, None)
        stale = [r["id"] for r in rows if r["is_active"] and (r["id"] not in CACHE or now - CACHE[r["id"]]["mono"] > CACHE_TTL_SEC)]
        if stale:
            done = time.monotonic()
            for res in await read_many(stale):
                CACHE[res["plc_id"]] = {"mono": done, "result": res}
        out = []
        for r in rows:
            if r["is_active"] and r["id"] in CACHE:
                out.append(dict(CACHE[r["id"]]["result"]))
            else:
                out.append(_failed(r["id"], time.time()))    # выключенный из опроса ПЛК не читается
        return out
