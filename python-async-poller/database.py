# ==============================================================================
# 1. ИМПОРТЫ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================================================================

# 1.1 Системные импорты и аннотации
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

# 1.2 Сторонние библиотеки
import asyncpg

# 1.3 Локальные модули проекта
from config import Settings
from models import AlarmEvent, DigitalTelemetryPoint, Plc, Tag, TelemetryPoint

log = logging.getLogger(__name__)


# 1.4 Вспомогательные функции времени
def utcnow() -> datetime:
    """Возвращает текущее время в формате UTC с часовым поясом."""
    return datetime.now(timezone.utc)


# ==============================================================================
# 2. SQL-ЗАПРОСЫ (SQL QUERIES)
# ==============================================================================

# 2.1 Чтение конфигурации ПЛК и тегов
_LOAD_PLCS = """
SELECT id, name, opc_endpoint, poll_interval_ms, username, password
FROM plcs
WHERE is_active = TRUE
ORDER BY id
"""

_LOAD_TAGS = """
SELECT id, plc_id, name, node_id, tag_type, is_alarm_enabled, alarm_message, deadband, limit_low, limit_high
FROM plc_tags
WHERE plc_id = $1 AND is_active = TRUE
ORDER BY id
"""

# Если API ещё не добавил колонки порогов (поллер стартовал раньше), теги грузятся по-старому, без мёртвой зоны
_LOAD_TAGS_LEGACY = """
SELECT id, plc_id, name, node_id, tag_type, is_alarm_enabled, alarm_message,
       NULL::double precision AS deadband, NULL::double precision AS limit_low, NULL::double precision AS limit_high
FROM plc_tags
WHERE plc_id = $1 AND is_active = TRUE
ORDER BY id
"""

# 2.2 Вставка телеметрии и аварийных событий
_INSERT_TELEMETRY = """
INSERT INTO telemetry (ts, plc_id, tag_id, value)
SELECT x.ts, x.plc_id, x.tag_id, x.value
FROM unnest($1::timestamptz[], $2::int[], $3::int[], $4::float8[])
    AS x(ts, plc_id, tag_id, value)
"""

_INSERT_ALARMS = """
INSERT INTO alarms_log (ts, plc_id, tag_id, alarm_message, value)
SELECT x.ts, x.plc_id, x.tag_id, x.alarm_message, x.value
FROM unnest($1::timestamptz[], $2::int[], $3::int[], $4::text[], $5::bool[])
    AS x(ts, plc_id, tag_id, alarm_message, value)
"""

_INSERT_DIGITAL_TELEMETRY = """
INSERT INTO digital_telemetry (ts, plc_id, tag_id, value)
SELECT x.ts, x.plc_id, x.tag_id, x.value
FROM unnest($1::timestamptz[], $2::int[], $3::int[], $4::bool[])
    AS x(ts, plc_id, tag_id, value)
"""

_UPSERT_DIGITAL_STATE = """
INSERT INTO digital_tag_state
    (tag_id, plc_id, value, is_readable, last_successful_read, last_error, updated_at)
VALUES ($1, $2, $3, TRUE, $4, NULL, NOW())
ON CONFLICT (tag_id) DO UPDATE SET
    plc_id = EXCLUDED.plc_id,
    value = EXCLUDED.value,
    is_readable = TRUE,
    last_successful_read = EXCLUDED.last_successful_read,
    last_error = NULL,
    updated_at = NOW()
"""

_MARK_DIGITAL_UNREADABLE = """
UPDATE digital_tag_state
SET is_readable = FALSE, last_error = $2, updated_at = NOW()
WHERE tag_id = $1
"""

_TOUCH_DIGITAL_STATE = """
UPDATE digital_tag_state
SET last_successful_read = NOW(), updated_at = NOW()
WHERE tag_id = $1 AND is_readable = TRUE
"""

_UPSERT_TAG_STATUS = """
INSERT INTO plc_tag_status (tag_id, is_readable, last_successful_read, last_error, updated_at)
VALUES ($1, $2, CASE WHEN $2 THEN NOW() ELSE NULL END, $3, NOW())
ON CONFLICT (tag_id) DO UPDATE SET
    is_readable = EXCLUDED.is_readable,
    last_successful_read = COALESCE(
        EXCLUDED.last_successful_read,
        plc_tag_status.last_successful_read
    ),
    last_error = EXCLUDED.last_error,
    updated_at = NOW()
"""

# 2.3 Обновление статусов подключений (ПЛК и Микросервисов)
_UPSERT_PLC_CONNECTION_STATUS = """
INSERT INTO plc_connection_status (plc_id, is_connected, last_successful_poll, last_error, updated_at)
VALUES ($1, $2, CASE WHEN $2 THEN NOW() ELSE NULL END, $3, NOW())
ON CONFLICT (plc_id) DO UPDATE SET
    is_connected = EXCLUDED.is_connected,
    last_successful_poll = COALESCE(EXCLUDED.last_successful_poll, plc_connection_status.last_successful_poll),
    last_error = EXCLUDED.last_error,
    updated_at = NOW()
"""

# История связи: событие пишется только при смене состояния (потеря и восстановление), а не при каждом опросе
_INSERT_LINK_EVENT = """
INSERT INTO plc_link_events (plc_id, state, reason)
SELECT $1, $2, $3
WHERE COALESCE((SELECT state FROM plc_link_events WHERE plc_id = $1 ORDER BY id DESC LIMIT 1), '') <> $2
"""

_UPSERT_SERVICE_CONNECTION_STATUS = """
INSERT INTO service_connection_status (service_name, is_connected, response_time_ms, last_error, updated_at)
VALUES ($1, $2, $3, $4, NOW())
ON CONFLICT (service_name) DO UPDATE SET
    is_connected = EXCLUDED.is_connected,
    response_time_ms = EXCLUDED.response_time_ms,
    last_error = EXCLUDED.last_error,
    updated_at = NOW()
"""

_SAVE_DISCOVERED_NODES = """
INSERT INTO plc_discovered_nodes
    (plc_id, db_name, variable_name, node_id, namespace_index, node_class, data_type, browse_path, is_system, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
ON CONFLICT (plc_id, node_id) DO UPDATE SET
    db_name = EXCLUDED.db_name,
    variable_name = EXCLUDED.variable_name,
    namespace_index = EXCLUDED.namespace_index,
    node_class = EXCLUDED.node_class,
    data_type = EXCLUDED.data_type,
    browse_path = EXCLUDED.browse_path,
    is_system = EXCLUDED.is_system,
    updated_at = NOW()
"""



# ==============================================================================
# 3. КЛАСС ВЗАИМОДЕЙСТВИЯ С БАЗОЙ ДАННЫХ (DATABASE)
# ==============================================================================
class Database:
    """Класс для управления пулом соединений asyncpg и выполнения операций с БД."""

    # 3.1 Инициализация и управление пулом соединений
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Создание пула подключений к PostgreSQL."""
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.dsn,
            min_size=self._settings.postgres_min_pool,
            max_size=self._settings.postgres_max_pool,
            command_timeout=30,
        )
        log.info("Подключение к PostgreSQL установлено")

    async def close(self) -> None:
        """Корректное закрытие пула подключений."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def get_pool(self) -> asyncpg.Pool:
        """Пул соединений для модулей, которым нужны собственные запросы (например, сборщик KPI)."""
        return self._pool_or_fail()

    def _pool_or_fail(self) -> asyncpg.Pool:
        """Проверка наличия активного пула соединений."""
        if self._pool is None:
            raise RuntimeError("Пул PostgreSQL не инициализирован")
        return self._pool

    # 3.2 Методы загрузки конфигурации
    async def load_active_plcs(self) -> list[Plc]:
        """Загрузка списка всех активных ПЛК из БД."""
        rows = await self._pool_or_fail().fetch(_LOAD_PLCS)
        return [
            Plc(
                id=row["id"],
                name=row["name"],
                opc_endpoint=row["opc_endpoint"],
                poll_interval_ms=row["poll_interval_ms"],
                username=row["username"],
                password=row["password"],
            )
            for row in rows
        ]

    async def load_tags(self, plc_id: int) -> list[Tag]:
        """Загрузка активных тегов для конкретного ПЛК."""
        try:
            rows = await self._pool_or_fail().fetch(_LOAD_TAGS, plc_id)
        except asyncpg.UndefinedColumnError:
            rows = await self._pool_or_fail().fetch(_LOAD_TAGS_LEGACY, plc_id)
        return [
            Tag(
                id=row["id"],
                plc_id=row["plc_id"],
                name=row["name"],
                node_id=row["node_id"],
                tag_type=row["tag_type"].upper(),
                is_alarm_enabled=row["is_alarm_enabled"],
                alarm_message=row["alarm_message"],
                deadband=row["deadband"],
                limit_low=row["limit_low"],
                limit_high=row["limit_high"],
            )
            for row in rows
        ]

    # 3.3 Методы записи телеметрии и событий
    async def insert_telemetry(self, points: list[TelemetryPoint]) -> None:
        """Пакетная вставка телеметрических данных."""
        if not points:
            return
        await self._pool_or_fail().execute(
            _INSERT_TELEMETRY,
            [p.ts for p in points],
            [p.plc_id for p in points],
            [p.tag_id for p in points],
            [p.value for p in points],
        )

    async def insert_alarms(self, events: list[AlarmEvent]) -> None:
        """Пакетная вставка аварийных событий."""
        if not events:
            return
        await self._pool_or_fail().execute(
            _INSERT_ALARMS,
            [e.ts for e in events],
            [e.plc_id for e in events],
            [e.tag_id for e in events],
            [e.alarm_message for e in events],
            [e.value for e in events],
        )

    async def insert_digital_telemetry(
        self, points: list[DigitalTelemetryPoint]
    ) -> None:
        """Пакетно сохраняет каждое успешно прочитанное DIGITAL-значение."""
        if not points:
            return
        await self._pool_or_fail().execute(
            _INSERT_DIGITAL_TELEMETRY,
            [point.ts for point in points],
            [point.plc_id for point in points],
            [point.tag_id for point in points],
            [point.value for point in points],
        )

    async def upsert_digital_states(
        self, points: list[DigitalTelemetryPoint]
    ) -> None:
        if not points:
            return
        await self._pool_or_fail().executemany(
            _UPSERT_DIGITAL_STATE,
            [(point.tag_id, point.plc_id, point.value, point.ts) for point in points],
        )

    async def mark_digital_unreadable(
        self, tag_ids: list[int], error: str
    ) -> None:
        if not tag_ids:
            return
        await self._pool_or_fail().executemany(
            _MARK_DIGITAL_UNREADABLE,
            [(tag_id, error) for tag_id in tag_ids],
        )

    async def touch_digital_states(self, tag_ids: list[int]) -> None:
        if not tag_ids:
            return
        await self._pool_or_fail().executemany(
            _TOUCH_DIGITAL_STATE, [(tag_id,) for tag_id in tag_ids]
        )

    async def update_tag_statuses(
        self, statuses: list[tuple[int, bool, str | None]]
    ) -> None:
        """Сохраняет результат последнего чтения каждого тега."""
        if not statuses:
            return
        await self._pool_or_fail().executemany(_UPSERT_TAG_STATUS, statuses)

    # 3.4 Методы обновления статусов здоровья и связи
    async def update_connection_status(
        self, plc_id: int, is_connected: bool, last_error: str | None = None
    ) -> None:
        """Сохранение или обновление статуса связи ПЛК в таблице plc_connection_status."""
        pool = self._pool_or_fail()
        await pool.execute(
            _UPSERT_PLC_CONNECTION_STATUS,
            plc_id,
            is_connected,
            last_error,
        )
        try:
            await pool.execute(
                _INSERT_LINK_EVENT, plc_id, "ok" if is_connected else "no_link", (last_error or "")[:500]
            )
        except asyncpg.UndefinedTableError:
            log.debug("Таблица plc_link_events ещё не создана (её создаёт API при запуске)")

    async def update_service_status(
        self,
        service_name: str,
        is_connected: bool,
        response_time_ms: float,
        error_msg: str | None = None,
    ) -> None:
        """Сохранение или обновление статуса связи микросервиса в таблице service_connection_status."""
        await self._pool_or_fail().execute(
            _UPSERT_SERVICE_CONNECTION_STATUS,
            service_name,
            is_connected,
            response_time_ms,
            error_msg,
        )

    async def save_discovered_nodes(
        self, plc_id: int, nodes: list[dict[str, Any]]
    ) -> None:
        """Сохранение/обновление обнаруженных OPC UA узлов в базе данных."""
        if not nodes:
            return
        await self._pool_or_fail().executemany(
            _SAVE_DISCOVERED_NODES,
            [
                (
                    plc_id,
                    n.get("db_name", ""),
                    n.get("variable_name", ""),
                    n.get("node_id", ""),
                    n.get("namespace_index", 0),
                    n.get("node_class", "Variable"),
                    n.get("data_type", "Unknown"),
                    n.get("browse_path", ""),
                    n.get("is_system", False),
                )
                for n in nodes
            ],
        )