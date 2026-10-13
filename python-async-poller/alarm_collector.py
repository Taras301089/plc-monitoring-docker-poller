"""Сборщик аварий: биты слов массива MSG.W (UInt16) превращаются в события alarm_events (появление и снятие).

Опрос раз в секунду, запись в базу только при смене состояния бита. Короткие импульсы короче периода опроса могут быть пропущены.
Таблицы создаёт API при запуске; пока их нет, сборщик молча ждёт."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import asyncpg

log = logging.getLogger(__name__)

_COLLECT_SEC = 1.0


def words_to_bits(words: list[Any], word_count: int) -> set[int]:
    """Номера активных битов: бит i лежит в слове i // 16, разряд i % 16; значения приводятся к 16 битам."""
    active: set[int] = set()
    for w_idx, w in enumerate(words[:word_count]):
        value = (w & 0xFFFF) if isinstance(w, int) and not isinstance(w, bool) else 0
        for b in range(16):
            if (value >> b) & 1:
                active.add(w_idx * 16 + b)
    return active


def diff_active(open_bits: set[int], active_bits: set[int]) -> tuple[list[int], list[int]]:
    """(появились, сняты): сравнение открытых событий с текущими активными битами."""
    return sorted(active_bits - open_bits), sorted(open_bits - active_bits)


_INSERT_OPEN = """
INSERT INTO alarm_events (plc_id, bit_index, started_at, active_at_start)
VALUES ($1, $2, $3, $4)
ON CONFLICT (plc_id, bit_index) WHERE ended_at IS NULL DO NOTHING
"""
_CLOSE = "UPDATE alarm_events SET ended_at = $3 WHERE plc_id = $1 AND bit_index = $2 AND ended_at IS NULL"
_LOAD_OPEN = "SELECT bit_index FROM alarm_events WHERE plc_id = $1 AND ended_at IS NULL"
_LOAD_SOURCES = "SELECT plc_id, node_id, word_count FROM alarm_sources WHERE enabled"


async def apply_changes(pool: asyncpg.Pool, plc_id: int, opened: list[int], closed: list[int], now: datetime, first: bool) -> None:
    """Пишет изменения: новые аварии (active_at_start при первом чтении после запуска) и снятия."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            if opened:
                await conn.executemany(_INSERT_OPEN, [(plc_id, b, now, first) for b in opened])
            if closed:
                await conn.executemany(_CLOSE, [(plc_id, b, now) for b in closed])


class AlarmCollector:
    def __init__(
        self,
        get_pool: Callable[[], asyncpg.Pool],
        get_clients: Callable[[], dict[int, Any]],
        stop: asyncio.Event | None,
    ) -> None:
        self._get_pool = get_pool
        self._get_clients = get_clients
        self._stop = stop
        self._open: dict[int, set[int]] = {}      # открытые аварии по ПЛК (кэш, чтобы не читать базу каждую секунду)
        self._warned = False

    async def run(self) -> None:
        log.info("Сборщик аварий запущен: период %s с", _COLLECT_SEC)
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.cycle_once()
            except Exception:
                log.exception("Сбой цикла сборщика аварий")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_COLLECT_SEC)
            except asyncio.TimeoutError:
                pass

    async def cycle_once(self, now: datetime | None = None) -> None:
        pool = self._get_pool()
        try:
            sources = await pool.fetch(_LOAD_SOURCES)
        except asyncpg.UndefinedTableError:
            if not self._warned:
                log.warning("Таблицы аварий ещё не созданы (их создаёт API при запуске)")
                self._warned = True
            return
        now = now or datetime.now(timezone.utc)
        clients = self._get_clients()
        for src in sources:
            plc_id = src["plc_id"]
            client = clients.get(plc_id)
            if client is None:
                continue
            try:
                words = await client.read_words(src["node_id"], src["word_count"])
            except Exception as exc:                  # нет связи или узел недоступен: состояние не меняем
                log.debug("ПЛК %s: массив аварий не прочитан: %s", plc_id, exc)
                continue
            active = words_to_bits(words, src["word_count"])
            first = plc_id not in self._open
            try:
                if first:
                    self._open[plc_id] = {r["bit_index"] for r in await pool.fetch(_LOAD_OPEN, plc_id)}
                opened, closed = diff_active(self._open[plc_id], active)
                if opened or closed:
                    await apply_changes(pool, plc_id, opened, closed, now, first)
                    self._open[plc_id] = set(active)
            except asyncpg.UndefinedTableError:
                self._open.pop(plc_id, None)
