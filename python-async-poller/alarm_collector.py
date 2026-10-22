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
_GAP_SEC = 5.0          # разрыв чтения длиннее: снятие найдено «вслепую», время снятия неточное
_SAVE_READ_SEC = 30.0   # как часто сохранять в базе время последнего удачного чтения (alarm_sources.last_read_at)
_WARN_EVERY_SEC = 300.0  # ошибка чтения массива аварий: первая сразу, затем напоминание не чаще раза в 5 минут


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
_CLOSE = (
    "UPDATE alarm_events SET ended_at = GREATEST($3, started_at), closed_in_gap = $4 "
    "WHERE plc_id = $1 AND bit_index = $2 AND ended_at IS NULL"
)
_SAVE_READ = "UPDATE alarm_sources SET last_read_at = $2 WHERE plc_id = $1"
_LOAD_OPEN = "SELECT bit_index FROM alarm_events WHERE plc_id = $1 AND ended_at IS NULL"
_LOAD_SOURCES = "SELECT plc_id, node_id, word_count, last_read_at FROM alarm_sources WHERE enabled"


async def apply_changes(
    pool: asyncpg.Pool, plc_id: int, opened: list[int], closed: list[int], now: datetime, first: bool,
    closed_at: datetime | None = None, closed_in_gap: bool = False,
) -> None:
    """Пишет изменения: новые аварии (active_at_start при первом чтении после запуска) и снятия.
    Снятие после разрыва чтения: closed_at = последнее удачное чтение, closed_in_gap = True (время снятия неточное)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            if opened:
                await conn.executemany(_INSERT_OPEN, [(plc_id, b, now, first) for b in opened])
            if closed:
                await conn.executemany(_CLOSE, [(plc_id, b, closed_at or now, closed_in_gap) for b in closed])


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
        self._last_ok: dict[int, datetime] = {}      # время последнего удачного чтения по ПЛК (для границы разрыва)
        self._saved_at: dict[int, datetime] = {}     # когда оно в последний раз записано в базу
        self._warn_at: dict[int, datetime] = {}      # когда в последний раз писали предупреждение об ошибке чтения

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
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
            if not self._warned:
                log.warning("Таблицы аварий ещё не созданы или не обновлены (их создаёт API при запуске)")
                self._warned = True
            return
        now = now or datetime.now(timezone.utc)
        clients = self._get_clients()
        for src in sources:
            plc_id = src["plc_id"]
            client = clients.get(plc_id)
            if client is None:
                # ПЛК снят с опроса: ничего не меняем; открытые аварии закроются, когда он вернётся (с пометкой «≈», время последнего чтения)
                continue
            try:
                words = await client.read_words(src["node_id"], src["word_count"])
            except Exception as exc:                  # нет связи или узел недоступен: состояние не меняем
                last_warn = self._warn_at.get(plc_id)
                if last_warn is None or (now - last_warn).total_seconds() >= _WARN_EVERY_SEC:
                    self._warn_at[plc_id] = now
                    log.warning("ПЛК %s: массив аварий не прочитан: %s", plc_id, exc)
                else:
                    log.debug("ПЛК %s: массив аварий не прочитан: %s", plc_id, exc)
                continue
            self._warn_at.pop(plc_id, None)
            active = words_to_bits(words, src["word_count"])
            first = plc_id not in self._open
            prev_ok = self._last_ok.get(plc_id) or src["last_read_at"]   # после перезапуска поллера берём из базы
            gap = prev_ok is not None and (now - prev_ok).total_seconds() > _GAP_SEC
            try:
                if first:
                    self._open[plc_id] = {r["bit_index"] for r in await pool.fetch(_LOAD_OPEN, plc_id)}
                opened, closed = diff_active(self._open[plc_id], active)
                if opened or closed:
                    await apply_changes(pool, plc_id, opened, closed, now, first,
                                        closed_at=prev_ok if gap else now, closed_in_gap=gap)
                    self._open[plc_id] = set(active)
                self._last_ok[plc_id] = now
                saved = self._saved_at.get(plc_id)
                if saved is None or (now - saved).total_seconds() >= _SAVE_READ_SEC:
                    await pool.execute(_SAVE_READ, plc_id, now)
                    self._saved_at[plc_id] = now
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                self._open.pop(plc_id, None)
