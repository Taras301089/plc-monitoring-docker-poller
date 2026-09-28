# ==============================================================================
# 1. ИМПОРТЫ И КОНСТАНТЫ
# ==============================================================================
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaError

from models import Plc, Tag, TagReading

log = logging.getLogger(__name__)

# Параметры экспоненциальной задержки (backoff) для переподключения
_BACKOFF_START_SEC = 1.0
_BACKOFF_MAX_SEC = 60.0
_CONNECT_TIMEOUT_SEC = 10.0
_READ_TIMEOUT_SEC = 10.0


# ==============================================================================
# 2. КЛИЕНТ OPC UA ДЛЯ КОНКРЕТНОГО ПЛК
# ==============================================================================
class PlcOpcClient:
    """Единственный контур связи с OPC UA сервером конкретного ПЛК.

    Обрывы канала не пробрасываются наружу как фатальные: вызывающий код
    получает пустой результат и продолжает цикл опроса.
    """

    # --------------------------------------------------------------------------
    # 2.1. Инициализация и управление состоянием
    # --------------------------------------------------------------------------
    def __init__(self, plc: Plc) -> None:
        self._plc = plc
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._backoff = _BACKOFF_START_SEC
        self._needs_reconnect = False

    @property
    def plc(self) -> Plc:
        return self._plc

    def update_plc(self, plc: Plc) -> None:
        """Обновление конфигурации ПЛК «на лету» (endpoint, учетные данные)."""
        endpoint_changed = plc.opc_endpoint != self._plc.opc_endpoint
        creds_changed = (
            plc.username != self._plc.username or plc.password != self._plc.password
        )
        self._plc = plc
        if endpoint_changed or creds_changed:
            self._needs_reconnect = True

    # --------------------------------------------------------------------------
    # 2.2. Управление соединением (Подключение / Отключение)
    # --------------------------------------------------------------------------
    async def disconnect(self) -> None:
        """Безопасное внешнее отключение от OPC UA сервера."""
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        """Внутренний метод закрытия сессии без захвата мьютекса."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.disconnect()
        except Exception:
            log.debug("ПЛК %s: ошибка при закрытии сессии OPC UA", self._plc.name, exc_info=True)

    async def ensure_connected(self) -> bool:
        """Проверка соединения и выполнение подключения/переподключения с backoff."""
        async with self._lock:
            if self._needs_reconnect:
                await self._disconnect_unlocked()
                self._needs_reconnect = False
            if self._client is not None:
                return True

            try:
                client = Client(url=self._plc.opc_endpoint, timeout=_CONNECT_TIMEOUT_SEC)
                if self._plc.username:
                    client.set_user(self._plc.username)
                    client.set_password(self._plc.password or "")
                await client.connect()
                self._client = client
                self._backoff = _BACKOFF_START_SEC
                log.info("ПЛК %s: OPC UA подключен (%s)", self._plc.name, self._plc.opc_endpoint)
                return True
            except Exception as exc:
                log.warning(
                    "ПЛК %s: нет связи с OPC UA (%s). Повтор через %.0f с",
                    self._plc.name,
                    exc,
                    self._backoff,
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2.0, _BACKOFF_MAX_SEC)
                return False

    # --------------------------------------------------------------------------
    # 2.3. Чтение тегов с обработкой сетевых исключений
    # --------------------------------------------------------------------------
    async def read_tags(self, tags: list[Tag]) -> list[TagReading]:
        """Пакетное чтение списка тегов с обработкой сбоев связи."""
        if not await self.ensure_connected():
            raise ConnectionError("OPC UA connection is unavailable")
        if not tags:
            try:
                client = self._client
                if client is None:
                    raise ConnectionError("OPC UA client is unavailable")
                await asyncio.wait_for(
                    client.get_node("i=2256").read_value(),
                    timeout=_READ_TIMEOUT_SEC,
                )
            except (asyncio.TimeoutError, ConnectionError, OSError, UaError) as exc:
                await self.disconnect()
                raise ConnectionError("OPC UA health check failed") from exc
            return []

        try:
            client = self._client
            if client is None:
                return []
            nodes = [client.get_node(tag.node_id) for tag in tags]
            # asyncua client.read_values принимает список объектов Node
            values = await asyncio.wait_for(
                client.read_values(nodes), timeout=_READ_TIMEOUT_SEC
            )
            now = datetime.now(timezone.utc)
            readings: list[TagReading] = []
            for tag, val in zip(tags, values, strict=False):
                readings.append(
                    TagReading(
                        tag=tag,
                        value=val,
                        source_ts=now,
                        is_good=val is not None,
                    )
                )
            return readings
        except (asyncio.TimeoutError, ConnectionError, OSError, UaError) as exc:
            log.warning("ПЛК %s: сбой чтения OPC UA (%s), переподключение", self._plc.name, exc)
            await self.disconnect()
            raise ConnectionError("OPC UA read failed") from exc

    async def browse_variables(
        self, include_system: bool = False, db_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """Оптимизированный обход OPC UA с использованием пакетного запроса ссылок."""
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")

            result: list[dict[str, Any]] = []
            db_value = db_filter.casefold() if db_filter else None
            visited: set[str] = set()

            # Пакетный обход узлов уровнем за уровнем
            queue: list[tuple[Any, list[str]]] = [(client.get_objects_node(), [])]

            while queue:
                # Берем пакет узлов для запроса ссылок
                batch = queue[:50]
                queue = queue[50:]

                tasks = []
                for node, path in batch:
                    tasks.append(
                        asyncio.wait_for(
                            node.get_references(
                                refs=ua.ObjectIds.HierarchicalReferences,
                                direction=ua.BrowseDirection.Forward,
                            ),
                            timeout=5.0,
                        )
                    )

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for (node, path), ref_res in zip(batch, results, strict=False):
                    if isinstance(ref_res, Exception) or not ref_res:
                        continue

                    for reference in ref_res:
                        child_node_id = reference.NodeId.to_string()
                        if child_node_id in visited:
                            continue
                        visited.add(child_node_id)

                        if not include_system and reference.NodeId.NamespaceIndex == 0:
                            continue

                        browse_name = reference.BrowseName.Name or ""
                        current_path = [*path, browse_name]
                        node_class = reference.NodeClass

                        if (
                            not include_system
                            and len(current_path) == 2
                            and current_path[1] in {"Server", "ServerCapabilities", "Types", "Views", "Aliases"}
                        ):
                            continue

                        current_matched_db = (
                            db_value is None or db_value in browse_name.casefold() or any(db_value in p.casefold() for p in current_path)
                        )

                        if node_class == ua.NodeClass.Variable:
                            if current_matched_db:
                                db_name = current_path[1] if len(current_path) > 2 else ""
                                is_system = (
                                    reference.NodeId.NamespaceIndex == 0
                                    or db_name in {"Server", "ServerCapabilities"}
                                    or "opcfoundation.org/UA" in db_name
                                )
                                result.append(
                                    {
                                        "db_name": db_name,
                                        "variable_name": browse_name,
                                        "node_id": child_node_id,
                                        "namespace_index": reference.NodeId.NamespaceIndex,
                                        "node_class": "Variable",
                                        "data_type": "Unknown",
                                        "browse_path": ".".join(current_path),
                                        "is_system": is_system,
                                    }
                                )
                        elif node_class in (ua.NodeClass.Object, ua.NodeClass.View):
                            # Отсечение ветвей: если задан db_filter, спускаемся только в подходящие ветки
                            if db_value is None or current_matched_db or len(current_path) <= 2:
                                child_node = client.get_node(reference.NodeId)
                                queue.append((child_node, current_path))

            return [item for item in result if include_system or not item["is_system"]]

    async def _ensure_connected_unlocked(self) -> bool:
        if self._needs_reconnect:
            await self._disconnect_unlocked()
            self._needs_reconnect = False
        if self._client is not None:
            return True
        try:
            client = Client(url=self._plc.opc_endpoint, timeout=_CONNECT_TIMEOUT_SEC)
            if self._plc.username:
                client.set_user(self._plc.username)
                client.set_password(self._plc.password or "")
            await client.connect()
            self._client = client
            self._backoff = _BACKOFF_START_SEC
            return True
        except Exception:
            return False


# ==============================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ КОНВЕРТАЦИИ ДАННЫХ
# ==============================================================================
def _to_reading(tag: Tag, dv: Any, fallback_ts: datetime) -> TagReading:
    """Преобразование сырого DataValue от OPC UA в доменную модель TagReading."""
    status = getattr(dv, "StatusCode", None)
    is_good = bool(status.is_good()) if status is not None else False
    variant = getattr(dv, "Value", None)
    value = getattr(variant, "Value", None) if variant is not None else None
    source_ts = getattr(dv, "SourceTimestamp", None) or fallback_ts
    if source_ts is not None and source_ts.tzinfo is None:
        source_ts = source_ts.replace(tzinfo=timezone.utc)
    return TagReading(tag=tag, value=value, source_ts=source_ts, is_good=is_good)