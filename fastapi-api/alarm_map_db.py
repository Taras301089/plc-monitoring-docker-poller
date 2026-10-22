"""Карта аварий в базе и в файле: таблица alarm_source_map, функция alarm_source() для ИИ-помощника и API «источник аварии» (S-14).

Карту строит alarm_map.py при сборке «Файла для ИИ» (tia.py): файл <logic>/<проект>.alarms.json и строки в таблице. Таблица хранит версии:
каждая сборка добавляет строки со своим built_from (имя ZIP выгрузки) и временем built_at; та же выгрузка заменяет свою версию, старше пяти
версий на ПЛК удаляются. Читает всегда последняя версия ПЛК; нет таблицы, строки или базы: берётся файл. stale = карта старше файла логики
(логику пересобрали, а карта не обновилась). Только чтение и запись своей таблицы: в ПЛК и в чужие таблицы ничего не пишется."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import require_user

router = APIRouter(prefix="/api/alarm-map", tags=["alarm-map"])

KEEP_VERSIONS = 5
MATCHED = ("text", "order", "none")
_pool: asyncpg.Pool | None = None
_loop: asyncio.AbstractEventLoop | None = None

DDL = """
CREATE TABLE IF NOT EXISTS alarm_source_map (
    id           SERIAL PRIMARY KEY,
    plc_id       INTEGER     NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    bit_index    INTEGER     NOT NULL,
    word_index   INTEGER     NOT NULL DEFAULT 0,
    bit_in_word  SMALLINT    NOT NULL DEFAULT 0,
    block        TEXT        NOT NULL DEFAULT '',
    net          INTEGER     NOT NULL DEFAULT 0,
    msg_type     TEXT        NOT NULL DEFAULT '',
    station_no   TEXT        NOT NULL DEFAULT '',
    source_expr  TEXT        NOT NULL DEFAULT '',
    precond_expr TEXT        NOT NULL DEFAULT '',
    latched      BOOLEAN     NOT NULL DEFAULT FALSE,
    reset_expr   TEXT        NOT NULL DEFAULT '',
    call_path    TEXT        NOT NULL DEFAULT '',
    matched_by   TEXT        NOT NULL DEFAULT 'none' CHECK (matched_by IN ('text', 'order', 'none')),
    built_from   TEXT        NOT NULL DEFAULT '',
    built_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
INDEX = "CREATE INDEX IF NOT EXISTS alarm_source_map_bit_idx ON alarm_source_map (plc_id, bit_index, built_at)"

COLS = ("plc_id", "bit_index", "word_index", "bit_in_word", "block", "net", "msg_type", "station_no", "source_expr", "precond_expr",
        "latched", "reset_expr", "call_path", "matched_by", "built_from", "built_at")
SELECT = ("SELECT bit_index, word_index, bit_in_word, block, net, msg_type, station_no, source_expr, precond_expr, latched, reset_expr, "
          "call_path, matched_by, built_from, built_at FROM alarm_source_map")


async def ensure_alarm_map_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(DDL)
    await pool.execute(INDEX)


def bind(pool: asyncpg.Pool | None, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Пул и цикл событий сайта: сборка «Файла для ИИ» идёт в потоке и ходит в базу через них (run_sync)."""
    global _pool, _loop
    _pool, _loop = pool, loop


def run_sync(coro: Any, timeout: float = 60.0) -> Any:
    """Выполнить сопрограмму на цикле сайта из рабочего потока; без привязки (тесты, командная строка) база недоступна."""
    if _loop is None or _pool is None:
        coro.close()
        raise RuntimeError("база не привязана к сборке")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def bound_pool() -> asyncpg.Pool | None:
    return _pool


def station_no(e: dict[str, Any]) -> str:
    """Номер станции или ячейки как строка: «2» для станции, «Ячейка 4» для ячейки; пусто, если неизвестно."""
    u = e.get("unit")
    if u is None:
        return ""
    return f"Ячейка {u}" if e.get("kind") == "cell" else str(u)


def entry_row(plc_id: int, e: dict[str, Any], built_from: str, built_at: datetime) -> tuple:
    return (plc_id, e["b"], e.get("w", e["b"] // 16), e.get("x", e["b"] % 16), e.get("blk", ""), int(e.get("net", 0)), e.get("type", ""),
            station_no(e), e.get("src", ""), e.get("pre", ""), bool(e.get("latched")), e.get("reset", ""), e.get("path", ""),
            e.get("by", "none") if e.get("by") in MATCHED else "none", built_from, built_at)


async def save_map(pool: asyncpg.Pool, plc_id: int, data: dict[str, Any], built_from: str, built_at: datetime | None = None) -> int:
    """Записывает версию карты: строки этой же выгрузки заменяются, версий старше KEEP_VERSIONS не остаётся. Возвращает число строк."""
    built_at = built_at or datetime.now(timezone.utc)
    rows = [entry_row(plc_id, e, built_from, built_at) for e in data["alarms"].values()]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM alarm_source_map WHERE plc_id = $1 AND built_from = $2", plc_id, built_from)
            await conn.copy_records_to_table("alarm_source_map", records=rows, columns=list(COLS))
            await conn.execute(
                "DELETE FROM alarm_source_map WHERE plc_id = $1 AND built_from IN ("
                " SELECT built_from FROM alarm_source_map WHERE plc_id = $1 GROUP BY built_from"
                " ORDER BY max(built_at) DESC OFFSET $2)", plc_id, KEEP_VERSIONS)
    return len(rows)


async def fetch_catalog(pool: asyncpg.Pool, plc_name: str) -> tuple[int | None, dict[int, str]]:
    """(plc_id, {бит: текст аварии}) из каталога аварий; нет такого ПЛК: (None, {})."""
    plc_id = await pool.fetchval("SELECT id FROM plcs WHERE lower(name) = lower($1)", plc_name)
    if plc_id is None:
        return None, {}
    rows = await pool.fetch("SELECT bit_index, text FROM alarm_catalog WHERE plc_id = $1 AND btrim(text) <> ''", plc_id)
    return plc_id, {int(r["bit_index"]): r["text"] for r in rows}


# ---------- чтение ----------
_file_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_file_lock = threading.Lock()


def logic_dir() -> Path:
    import tia          # здесь, а не наверху: tia импортирует этот модуль
    return tia.BRIDGE_DIR / "logic"


def _load_file(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        with _file_lock:
            hit = _file_cache.get(str(path))
            if hit and hit[0] == st.st_mtime:
                return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        with _file_lock:
            _file_cache[str(path)] = (st.st_mtime, data)
        return data
    except (OSError, ValueError):
        return None


def _file_for(plc_id: int) -> tuple[Path, dict[str, Any]] | None:
    d = logic_dir()
    try:
        files = sorted(d.glob("*.alarms.json"))
    except OSError:
        return None
    for f in files:
        data = _load_file(f)
        if data and (data.get("meta") or {}).get("plc_id") == plc_id:
            return f, data
    return None


def _logic_mtime(alarms_file: Path) -> datetime | None:
    p = alarms_file.with_name(alarms_file.name[: -len(".alarms.json")] + ".json")
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _public(e: dict[str, Any], built_from: str, built_at: datetime | None, stale: bool, src: str) -> dict[str, Any]:
    out = {"found": True, "from": src, "bit_index": e["b"], "word_index": e.get("w", e["b"] // 16), "bit_in_word": e.get("x", e["b"] % 16),
           "block": e.get("blk", ""), "net": e.get("net", 0), "net_title": e.get("nt", ""), "msg_type": e.get("type", ""),
           "station_no": station_no(e), "unit_name": e.get("unit_name", ""), "source_expr": e.get("src", ""), "precond_expr": e.get("pre", ""),
           "latched": bool(e.get("latched")), "reset_expr": e.get("reset", ""), "call_path": e.get("path", ""), "matched_by": e.get("by", "none"),
           "flags": e.get("flags", []), "chain": e.get("chain", []), "built_from": built_from,
           "built_at": built_at.isoformat() if built_at else None, "stale": stale}
    return out


def alarm_source_file(plc_id: int, bit_index: int) -> dict[str, Any] | None:
    """Источник аварии из файла <проект>.alarms.json (резерв, когда таблицы нет); None: файла или записи нет."""
    hit = _file_for(plc_id)
    if hit is None:
        return None
    f, data = hit
    e = data.get("alarms", {}).get(str(bit_index))
    if e is None:
        return None
    meta = data.get("meta", {})
    built = None
    if meta.get("built_at"):
        try:
            built = datetime.fromisoformat(meta["built_at"])
        except ValueError:
            built = None
    lm = _logic_mtime(f)
    return _public(e, meta.get("built_from", ""), built, bool(built and lm and lm > built), "file")


async def alarm_source(pool: asyncpg.Pool | None, plc_id: int, bit_index: int) -> dict[str, Any]:
    """Источник аварии для ИИ-помощника: последняя версия карты из таблицы, при отсутствии таблицы или строки из файла.
    Ответ всегда словарь: found False, если источника нет (ничего не выдумывается); built_from и stale (карта старше файла логики)."""
    row = None
    if pool is not None:
        try:
            row = await pool.fetchrow(
                SELECT + " WHERE plc_id = $1 AND bit_index = $2 AND built_at = (SELECT max(built_at) FROM alarm_source_map WHERE plc_id = $1)"
                " ORDER BY id DESC LIMIT 1", plc_id, bit_index)
        except asyncpg.PostgresError:
            row = None                                  # таблицы нет (ещё не создана) или база недоступна: берём файл
    if row is not None:
        f = _file_for(plc_id)
        lm = _logic_mtime(f[0]) if f else None
        built = row["built_at"]
        res = {"found": True, "from": "db", "bit_index": row["bit_index"], "word_index": row["word_index"], "bit_in_word": row["bit_in_word"],
               "block": row["block"], "net": row["net"], "msg_type": row["msg_type"], "station_no": row["station_no"],
               "source_expr": row["source_expr"], "precond_expr": row["precond_expr"], "latched": row["latched"],
               "reset_expr": row["reset_expr"], "call_path": row["call_path"], "matched_by": row["matched_by"],
               "built_from": row["built_from"], "built_at": built.isoformat(), "stale": bool(lm and lm > built)}
        if f:                                           # из файла добавляются подпись сети, флаги и цепочка условий (в таблице их нет)
            e = f[1].get("alarms", {}).get(str(bit_index))
            if e and f[1].get("meta", {}).get("built_from") == row["built_from"]:
                res.update({"net_title": e.get("nt", ""), "unit_name": e.get("unit_name", ""), "flags": e.get("flags", []), "chain": e.get("chain", [])})
        return res
    res = alarm_source_file(plc_id, bit_index)
    return res if res is not None else {"found": False, "plc_id": plc_id, "bit_index": bit_index, "stale": False}


@router.get("", summary="Источник аварии ПЛК: блок, сеть и условие, которое ставит бит (только вход)")
async def api_alarm_source(request: Request, plc_id: int = Query(..., ge=1), bit: int = Query(..., ge=0, le=79999),
                           _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    return await alarm_source(pool, plc_id, bit)
