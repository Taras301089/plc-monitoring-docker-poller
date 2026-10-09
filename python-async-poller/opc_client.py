# ==============================================================================
# 1. ИМПОРТЫ И КОНСТАНТЫ
# ==============================================================================
from __future__ import annotations

import asyncio
import logging
import os
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
        """Оптимизированный обход OPC UA с использованием пакетного запроса ссылок.
        Добавлено прямое получение Global Data Blocks по NodeID из Namespace 3."""
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")

            result: list[dict[str, Any]] = []
            db_value = db_filter.casefold() if db_filter else None
            visited: set[str] = set()

            # Сначала - обход Instance Data Blocks через дерево
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
                pending_vars: list[tuple[dict[str, Any], Any]] = []   # переменные пакета: тип (структура, массив) определяется по детям

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

                        # DEBUG: логируем ВСЕ узлы на уровне 1-2 независимо от типа
                        if len(current_path) <= 2:
                            log.error(f"🔹 ALL_Node L{len(current_path)+1} type={node_class.name}: {' > '.join(current_path)}")

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
                            # DEBUG: логируем ВСЕ переменные внутри DataBlocksGlobal
                            if len(current_path) > 1 and current_path[1] == "DataBlocksGlobal":
                                log.error(f"💾 DataBlocksGlobal VARIABLE: {' > '.join(current_path + [browse_name])}")
                            # логируем переменные содержащие "user"
                            if "user" in browse_name.lower():
                                log.error(f"⭐ НАЙДЕНА 'user' переменная: {' > '.join(current_path)}")
                            if current_matched_db:
                                db_name = current_path[1] if len(current_path) > 2 else ""
                                is_system = (
                                    reference.NodeId.NamespaceIndex == 0
                                    or db_name in {"Server", "ServerCapabilities"}
                                    or "opcfoundation.org/UA" in db_name
                                )
                                item = {
                                    "db_name": db_name,
                                    "variable_name": browse_name,
                                    "node_id": child_node_id,
                                    "namespace_index": reference.NodeId.NamespaceIndex,
                                    "node_class": "Variable",
                                    "data_type": "Unknown",
                                    "browse_path": ".".join(current_path),
                                    "is_system": is_system,
                                }
                                result.append(item)
                                if not is_system:
                                    pending_vars.append((item, reference))
                        elif node_class == ua.NodeClass.Object:
                            # DEBUG: логируем ВСЕ узлы на уровне 1-2 для диагностики структуры
                            if len(current_path) <= 2:
                                log.error(f"📍 OPC_UA_Structure Level {len(current_path)+1}: {' > '.join(current_path + [browse_name])}")
                            # Логируем ВСЕ узлы внутри DataBlocksGlobal
                            if len(current_path) > 1 and current_path[1] == "DataBlocksGlobal":
                                log.error(f"🔍 DataBlocksGlobal content: {' > '.join(current_path + [browse_name])}, depth={len(current_path)}")
                            # Дополнительно логируем узлы содержащие "Global" или "user"
                            if "global" in browse_name.lower() or "user" in browse_name.lower():
                                log.error(f"🎯 НАЙДЕН узел с Global/user: path={' > '.join(current_path + [browse_name])}, depth={len(current_path)}, type=Object")

                            # Сохранение Data Blocks - сохраняем ВСЕ Object узлы БЕЗ ОГРАНИЧЕНИЙ
                            # Это позволит найти блоки на любом уровне иерархии
                            is_system = reference.NodeId.NamespaceIndex == 0
                            result.append(
                                {
                                    "db_name": browse_name,
                                    "variable_name": "",
                                    "node_id": child_node_id,
                                    "namespace_index": reference.NodeId.NamespaceIndex,
                                    "node_class": "Object",
                                    "data_type": "DataBlockObject",
                                    "browse_path": ".".join(current_path + [browse_name]),
                                    "is_system": is_system,
                                }
                            )
                            # Продолжать спуск в другие Object/View узлы (расширен до уровня 10 для поиска вложенных структур)
                            if db_value is None or current_matched_db or len(current_path) <= 10:
                                child_node = client.get_node(reference.NodeId)
                                queue.append((child_node, current_path))
                        elif node_class == ua.NodeClass.View:
                            # DEBUG: логируем ВСЕ View узлы
                            log.error(f"👁️ VIEW_Node: {' > '.join(current_path + [browse_name])}, depth={len(current_path)}")
                            # Продолжать спуск в View узлы (расширен до уровня 10 для поиска вложенных структур)
                            if db_value is None or current_matched_db or len(current_path) <= 10:
                                child_node = client.get_node(reference.NodeId)
                                queue.append((child_node, current_path))

                if pending_vars:
                    kinds = await self._has_children_batch(client, [ref for _, ref in pending_vars])
                    for (item, _ref), kind in zip(pending_vars, kinds, strict=False):
                        if kind:
                            item["data_type"] = kind

            # ===== ДОПОЛНИТЕЛЬНО: Получение Global Data Blocks напрямую по NodeID (Namespace 3) =====
            # Папка DataBlocksGlobal отображается как пустая при стандартном обходе,
            # поэтому получаем Global Data Blocks напрямую по их NodeID
            try:
                global_dbs = await self._get_global_data_blocks_by_nodeid(client, db_value)
                result.extend(global_dbs)
                log.info(f"✅ Получено {len(global_dbs)} переменных из Global Data Blocks")
            except Exception as e:
                log.warning(f"⚠️ Не удалось получить Global Data Blocks по NodeID: {e}")

            return [item for item in result if include_system or not item["is_system"]]

    async def _discover_global_data_blocks(self, client: Client) -> list[str]:
        """Автоматическое обнаружение всех Global Data Blocks из Namespace 3.
        Возвращает список имён найденных Global Data Blocks.
        Оптимизировано для предотвращения бесконечных циклов в OPC UA графе."""
        global_dbs: set[str] = set()

        try:
            # Стратегия: ищем в Objects > Server > Namespaces > Index 3 и подобных местах
            # + также используем целевой поиск вместо полного BFS дерева
            root = client.get_root_node()
            visited: set[str] = set()
            queue = [(root, 0)]  # (node, depth)
            max_depth = 5  # Ограничиваем глубину поиска
            max_nodes = 1000  # Максимум узлов для обработки

            while queue and len(visited) < max_nodes:
                node, depth = queue.pop(0)

                try:
                    node_id = node.nodeid.to_string()
                    if node_id in visited:
                        continue
                    visited.add(node_id)

                    # Пропускаем глубокий поиск если уже нашли много блоков
                    if len(global_dbs) > 50:
                        break

                    # Получаем ссылки на дочерние узлы
                    try:
                        refs = await asyncio.wait_for(
                            node.get_references(
                                refs=ua.ObjectIds.HierarchicalReferences,
                                direction=ua.BrowseDirection.Forward,
                            ),
                            timeout=3.0,
                        )
                    except Exception:
                        continue

                    # Проверяем namespace текущего узла ПЕРЕД добавлением children
                    for ref in refs:
                        if ref.NodeId.NamespaceIndex == 3:
                            browse_name = ref.BrowseName.Name or ""
                            if browse_name and browse_name not in {"", "Icon"}:
                                # Исключаем Instance Data Blocks
                                # Instance DBs обычно содержат "Instance" в описании или имеют определённые паттерны
                                is_instance_db = False
                                try:
                                    # Пытаемся прочитать description узла
                                    child_node = client.get_node(ref.NodeId)
                                    desc = await asyncio.wait_for(child_node.read_description(), timeout=1.0)
                                    if desc and "Instance" in str(desc):
                                        is_instance_db = True
                                except Exception:
                                    pass

                                if not is_instance_db:
                                    global_dbs.add(browse_name)
                                    log.error(f"🔹 Global Data Block найден: {browse_name}")

                    # Продолжаем спуск ТОЛЬКО если ещё не достаточно глубоко
                    # И ТОЛЬКО для узлов которые ещё не посещали
                    if depth < max_depth:
                        for ref in refs:
                            ref_node_id = ref.NodeId.to_string()
                            if ref_node_id not in visited:
                                try:
                                    child_node = client.get_node(ref.NodeId)
                                    queue.append((child_node, depth + 1))
                                except Exception:
                                    pass

                except Exception:
                    continue

            log.error(f"✅ Автоматически найдено Global Data Blocks: {sorted(list(global_dbs))}")
            return sorted(list(global_dbs))

        except Exception as e:
            log.warning(f"⚠️ Не удалось автоматически обнаружить Global Data Blocks: {e}")
            return []

    async def _get_global_data_blocks_by_nodeid(
        self, client: Client, db_filter: str | None
    ) -> list[dict[str, Any]]:
        """Получение Global Data Blocks напрямую по NodeID из Namespace 3.
        Используется потому что при стандартном обходе дерева DataBlocksGlobal отображается как пустая."""
        result: list[dict[str, Any]] = []
        db_value = db_filter.casefold() if db_filter else None

        # Автоматическое получение Global Data Blocks вместо жёстко закодированного списка
        known_global_dbs = await self._discover_global_data_blocks(client)
        if not known_global_dbs:
            log.warning("⚠️ Global Data Blocks не найдены")
            return result

        for db_name in known_global_dbs:
            # Пропускаем если не совпадает с фильтром
            if db_value and db_value not in db_name.casefold():
                continue

            try:
                # NodeID для Global Data Block в формате: ns=3;s="ИмяБлока"
                nodeid_str = f'ns=3;s="{db_name}"'
                node = client.get_node(nodeid_str)

                # Пытаемся получить сам объект
                browse_name = await node.read_browse_name()
                log.info(f"✅ Найден Global Data Block: {db_name} (NodeID: {nodeid_str})")

                # Добавляем сам Data Block как Object
                result.append({
                    "db_name": db_name,
                    "variable_name": "",
                    "node_id": nodeid_str,
                    "namespace_index": 3,
                    "node_class": "Object",
                    "data_type": "DataBlockObject",
                    "browse_path": f"DataBlocksGlobal.{db_name}",
                    "is_system": False,
                })

                # Получаем переменные внутри Global Data Block
                try:
                    stack: list[tuple[Any, list[str]]] = [(node, [])]
                    while stack:
                        cur_node, sub_path = stack.pop()
                        if len(sub_path) > 8:
                            continue
                        references = await cur_node.get_references(
                            refs=ua.ObjectIds.HierarchicalReferences,
                            direction=ua.BrowseDirection.Forward,
                        )
                        var_refs = [r for r in references if r.NodeClass == ua.NodeClass.Variable]
                        has_kids = await self._has_children_batch(client, var_refs)
                        kids_by_id = {r.NodeId.to_string(): k for r, k in zip(var_refs, has_kids)}
                        for ref in references:
                            var_name = ref.BrowseName.Name or ""
                            if ref.NodeClass == ua.NodeClass.Variable:
                                result.append({
                                    "db_name": db_name,
                                    "variable_name": var_name,
                                    "node_id": ref.NodeId.to_string(),
                                    "namespace_index": ref.NodeId.NamespaceIndex,
                                    "node_class": "Variable",
                                    "data_type": kids_by_id.get(ref.NodeId.to_string()) or "Unknown",
                                    "browse_path": ".".join(["DataBlocksGlobal", db_name, *sub_path, var_name]),
                                    "is_system": False,
                                })
                            elif ref.NodeClass == ua.NodeClass.Object and ref.NodeId.NamespaceIndex == 3:
                                stack.append((client.get_node(ref.NodeId), [*sub_path, var_name]))
                except Exception as e:
                    log.warning(f"Не удалось получить переменные из {db_name}: {e}")

            except Exception as e:
                log.debug(f"⚠️ Global Data Block '{db_name}' не найден: {e}")

        return result

    async def _has_children_batch(self, client: Client, refs: list[Any]) -> list[str]:
        """Тип узла по его детям: 'Array' (элементы [n]), 'Structure tag' (поля) или '' (лист)."""
        async def one(ref: Any) -> str:
            try:
                kids = await asyncio.wait_for(
                    client.get_node(ref.NodeId).get_references(
                        refs=ua.ObjectIds.HierarchicalReferences,
                        direction=ua.BrowseDirection.Forward,
                    ),
                    timeout=5.0,
                )
                kids = [k for k in kids if k.NodeClass in (ua.NodeClass.Variable, ua.NodeClass.Object)]
                if not kids:
                    return ""
                if all(k.NodeId.to_string().endswith(f"[{k.BrowseName.Name}]") for k in kids):
                    return "Array"
                return "Structure tag"
            except Exception:
                return ""

        out: list[str] = []
        for i in range(0, len(refs), 50):
            out.extend(await asyncio.gather(*(one(r) for r in refs[i:i + 50])))
        return out

    async def read_values(self, node_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Пакетное чтение текущих значений узлов одним запросом (для живого просмотра в интерфейсе)."""
        out: dict[str, dict[str, Any]] = {}
        valid: list[tuple[str, ua.NodeId]] = []
        for raw in node_ids:
            try:
                valid.append((raw, ua.NodeId.from_string(raw)))
            except Exception:
                out[raw] = {"ok": False, "v": None, "t": "", "s": "BadNodeIdInvalid"}
        if not valid:
            return out
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")
            params = ua.ReadParameters()
            params.NodesToRead = [
                ua.ReadValueId(NodeId=nid, AttributeId=ua.AttributeIds.Value) for _, nid in valid
            ]
            try:
                results = await asyncio.wait_for(client.uaclient.read(params), timeout=5.0)
            except (asyncio.TimeoutError, ConnectionError, OSError, UaError) as exc:
                await self._disconnect_unlocked()
                raise ConnectionError("OPC UA read failed") from exc
        for (raw, _), dv in zip(valid, results, strict=False):
            status = getattr(dv, "StatusCode", None)
            variant = getattr(dv, "Value", None)
            good = bool(status.is_good()) if status is not None else False
            out[raw] = {
                "ok": good,
                "v": _to_jsonable(getattr(variant, "Value", None)) if good else None,
                "t": variant.VariantType.name if good and variant is not None else "",
                "s": "" if good else (status.name if status is not None else "Bad"),
            }
        return out

    async def write_int(self, node_id: str, value: int) -> dict[str, Any]:
        """Запись целого числа в одну переменную ПЛК с чтением значения до и после (для плана на день).

        Единственная точка записи в ПЛК: работает только при APP_ENV=prod (на сервере); в разработке запись запрещена."""
        if os.getenv("APP_ENV", "dev").strip().lower() != "prod":
            raise PermissionError("Режим разработки: запись в ПЛК отключена")
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")
            node = client.get_node(node_id)
            variant_type = await asyncio.wait_for(node.read_data_type_as_variant_type(), timeout=10.0)
            if variant_type not in (ua.VariantType.Int16, ua.VariantType.UInt16, ua.VariantType.Int32):
                raise ValueError(f"Тип переменной {variant_type.name} не подходит для записи плана")
            old = await asyncio.wait_for(node.read_value(), timeout=10.0)
            await asyncio.wait_for(node.write_value(ua.DataValue(ua.Variant(int(value), variant_type))), timeout=10.0)
            new = await asyncio.wait_for(node.read_value(), timeout=10.0)
        return {"old": _to_jsonable(old), "readback": _to_jsonable(new)}

    async def read_node_info(self, node_id: str) -> dict[str, str]:
        """Тип данных узла и тип тега для БД: Boolean -> DIGITAL, остальное -> ANALOG."""
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")
            variant_type = await asyncio.wait_for(
                client.get_node(node_id).read_data_type_as_variant_type(), timeout=10.0
            )
            return {
                "data_type": variant_type.name,
                "tag_type": "DIGITAL" if variant_type == ua.VariantType.Boolean else "ANALOG",
            }

    async def browse_children(self, node_id: str) -> list[dict[str, Any]]:
        """Прямые дочерние узлы (для раскрытия структурных тегов); имя, тип и признак вложенности."""
        async with self._lock:
            if not await self._ensure_connected_unlocked():
                raise ConnectionError("OPC UA connection is unavailable")
            client = self._client
            if client is None:
                raise ConnectionError("OPC UA client is unavailable")
            refs = await asyncio.wait_for(
                client.get_node(node_id).get_references(
                    refs=ua.ObjectIds.HierarchicalReferences,
                    direction=ua.BrowseDirection.Forward,
                ),
                timeout=10.0,
            )
            refs = [r for r in refs if r.NodeClass in (ua.NodeClass.Variable, ua.NodeClass.Object)]
            kids = await self._has_children_batch(client, refs)
            return [
                {
                    "variable_name": r.BrowseName.Name or "",
                    "node_id": r.NodeId.to_string(),
                    "namespace_index": r.NodeId.NamespaceIndex,
                    "node_class": r.NodeClass.name,
                    "data_type": k or "Unknown",
                }
                for r, k in zip(refs, kids)
            ]

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
def _to_jsonable(value: Any) -> Any:
    """Приведение значения OPC UA к JSON-совместимому виду."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(x) for x in value[:20]]
    return str(value)


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