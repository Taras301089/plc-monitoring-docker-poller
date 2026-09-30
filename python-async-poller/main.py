# ==============================================================================
# 1. ИМПОРТЫ И ИНИЦИАЛИЗАЦИЯ
# ==============================================================================
from __future__ import annotations

import asyncio
import logging
import signal
from uuid import uuid4
from contextlib import suppress

from aiohttp import web
from checker import TagChecker
from config import load_settings
from database import Database
from models import DigitalTelemetryPoint, Plc
from opc_client import PlcOpcClient

log = logging.getLogger(__name__)

_DIGITAL_HEARTBEAT_SEC = 30.0


# ==============================================================================
# 2. ОСНОВНОЙ СЕРВИС ОПРОСА (POLLER SERVICE)
# ==============================================================================
class PollerService:
    # --- Инициализация и управление состоянием ---
    def __init__(self, db: Database, config_reload_sec: int) -> None:
        self._db = db
        self._config_reload_sec = config_reload_sec
        self._stop = asyncio.Event()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._clients: dict[int, PlcOpcClient] = {}
        self._checkers: dict[int, TagChecker] = {}
        self._last_status: dict[int, bool] = {}
        self._digital_readable: dict[int, bool] = {}
        self._digital_heartbeat: dict[int, float] = {}
        self._browse_jobs: dict[str, asyncio.Task[list[dict[str, object]]]] = {}

    # --- Главный цикл сервиса ---
    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                start_time = asyncio.get_running_loop().time()
                try:
                    plcs = await self._db.load_active_plcs()
                    response_time_ms = (asyncio.get_running_loop().time() - start_time) * 1000.0

                    # Запись успешного пульса поллера в базу
                    await self._db.update_service_status(
                        service_name="plc-monitoring-docker-poller",
                        is_connected=True,
                        response_time_ms=response_time_ms,
                    )
                except Exception as e:
                    log.exception("Не удалось загрузить список ПЛК из PostgreSQL")
                    plcs = []
                                        # Фиксация сбоя подключения поллера к БД
                    await self._update_service_status_safe(
                        service_name="plc-monitoring-docker-poller",
                        is_connected=False,
                        response_time_ms=0.0,
                        error_msg=str(e),
                    )

                await self._reconcile(plcs)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(self._config_reload_sec, 1)
                    )
                except asyncio.TimeoutError:
                    pass

        finally:
            # Фиксация корректной остановки сервиса в базе данных
            await self._update_service_status_safe(
                service_name="plc-monitoring-docker-poller",
                is_connected=False,
                response_time_ms=0.0,
                error_msg="Service stopped gracefully",
            )

    async def _update_service_status_safe(
        self,
        service_name: str,
        is_connected: bool,
        response_time_ms: float,
        error_msg: str | None = None,
    ) -> None:
        try:
            await self._db.update_service_status(
                service_name=service_name,
                is_connected=is_connected,
                response_time_ms=response_time_ms,
                error_msg=error_msg,
            )
        except Exception:
            log.exception("Не удалось записать статус сервиса в PostgreSQL")

    def request_stop(self) -> None:
        self._stop.set()



    async def browse_plc(
        self, plc_id: int, include_system: bool, db_filter: str | None, name_filter: str | None
    ) -> list[dict[str, object]]:
        client = self._clients.get(plc_id)
        if client is None:
            raise LookupError("PLC is not active")
        variables = await client.browse_variables(
            include_system=include_system, db_filter=db_filter
        )
        if variables:
            try:
                await self._db.save_discovered_nodes(plc_id, variables)
            except Exception:
                log.exception("Не удалось сохранить список узлов в БД")

        db_value = db_filter.casefold() if db_filter else None
        name_value = name_filter.casefold() if name_filter else None
        return [
            variable
            for variable in variables
            if (not db_value or db_value in str(variable["browse_path"]).casefold())
            and (not name_value or name_value in str(variable["variable_name"]).casefold())
        ]







    def start_browse_job(
        self, plc_id: int, include_system: bool, db_filter: str | None, name_filter: str | None
    ) -> str:
        job_id = uuid4().hex
        self._browse_jobs[job_id] = asyncio.create_task(
            self.browse_plc(plc_id, include_system, db_filter, name_filter),
            name=f"browse-{plc_id}-{job_id}",
        )
        return job_id

    # --- Синхронизация активных задач ПЛК ---
    async def _reconcile(self, plcs: list[Plc]) -> None:
        active_ids = {plc.id for plc in plcs}

        # Остановка деактивированных ПЛК и очистка ресурсов
        for plc_id in list(self._tasks):
            if plc_id not in active_ids:
                log.info("ПЛК id=%s деактивирован, останавливаем опрос", plc_id)
                self._tasks[plc_id].cancel()
                with suppress(asyncio.CancelledError):
                    await self._tasks[plc_id]
                self._tasks.pop(plc_id, None)
                client = self._clients.pop(plc_id, None)
                if client is not None:
                    await client.disconnect()
                self._checkers.pop(plc_id, None)
                self._last_status.pop(plc_id, None)

        # Запуск новых и обновление существующих воркеров ПЛК
        for plc in plcs:
            if plc.id in self._tasks and not self._tasks[plc.id].done():
                self._clients[plc.id].update_plc(plc)
                continue
            self._clients[plc.id] = PlcOpcClient(plc)
            self._checkers[plc.id] = TagChecker()
            self._tasks[plc.id] = asyncio.create_task(
                self._poll_loop(plc.id),
                name=f"plc-{plc.id}-{plc.name}",
            )
            log.info(
                "Запущен опрос ПЛК %s (id=%s, interval=%s мс)",
                plc.name,
                plc.id,
                plc.poll_interval_ms,
            )

    # --- Цикл опроса конкретного ПЛК и отслеживание статуса связи ---
    async def _poll_loop(self, plc_id: int) -> None:
        while not self._stop.is_set():
            client = self._clients.get(plc_id)
            checker = self._checkers.get(plc_id)
            if client is None or checker is None:
                return

            interval = max(client.plc.poll_interval_ms, 50) / 1000.0
            started = asyncio.get_running_loop().time()
            try:
                await self._poll_once(client, checker)

                # Обновляем heartbeat и время последнего успешного чтения.
                await self._update_plc_status_safe(plc_id, is_connected=True)
                self._last_status[plc_id] = True

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("ПЛК %s: ошибка цикла опроса, сервис продолжает работу", client.plc.name)

                # Всегда синхронизируем БД: старый true не должен оставаться
                # из-за рассинхронизации состояния воркера и записи в БД.
                await self._update_plc_status_safe(
                    plc_id, is_connected=False, last_error=str(e)
                )
                self._last_status[plc_id] = False

            elapsed = asyncio.get_running_loop().time() - started
            delay = max(0.0, interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _update_plc_status_safe(
        self, plc_id: int, is_connected: bool, last_error: str | None = None
    ) -> None:
        try:
            await self._db.update_connection_status(
                plc_id, is_connected=is_connected, last_error=last_error
            )
        except Exception:
            log.exception("Не удалось записать статус ПЛК id=%s в PostgreSQL", plc_id)

    # --- Разовый итерационный опрос и запись данных в БД ---
    async def _poll_once(self, client: PlcOpcClient, checker: TagChecker) -> None:
        tags = await self._db.load_tags(client.plc.id)
        checker.retain_tags({tag.id for tag in tags})

        try:
            readings = await client.read_tags(tags)
        except Exception as exc:
            await self._db.update_tag_statuses(
                [(tag.id, False, str(exc)) for tag in tags]
            )
            await self._db.mark_digital_unreadable(
                [tag.id for tag in tags if tag.tag_type == "DIGITAL"], str(exc)
            )
            for tag in tags:
                if tag.tag_type == "DIGITAL":
                    self._digital_readable[tag.id] = False
            raise

        readings_by_tag = {reading.tag.id: reading for reading in readings}
        await self._db.update_tag_statuses(
            [
                (
                    tag.id,
                    reading is not None and reading.is_good,
                    None if reading is not None and reading.is_good else "Bad OPC UA quality or no response",
                )
                for tag in tags
                for reading in [readings_by_tag.get(tag.id)]
            ]
        )
        bad_digital_ids = [
            tag.id
            for tag in tags
            if tag.tag_type == "DIGITAL"
            and not any(reading.tag.id == tag.id and reading.is_good for reading in readings)
        ]
        await self._db.mark_digital_unreadable(
            bad_digital_ids, "Bad OPC UA quality or no response"
        )
        telemetry, digital_states, digital_telemetry, alarms = checker.process(readings)
        readable_digital_ids = {
            reading.tag.id
            for reading in readings
            if reading.tag.tag_type == "DIGITAL" and reading.is_good
        }
        recovered_states = [
            reading
            for reading in readings
            if reading.tag.id in readable_digital_ids
            and not self._digital_readable.get(reading.tag.id, False)
        ]
        digital_states.extend(
            DigitalTelemetryPoint(
                plc_id=reading.tag.plc_id,
                tag_id=reading.tag.id,
                value=bool(reading.value),
                ts=reading.source_ts,
            )
            for reading in recovered_states
            if reading.tag.id not in {point.tag_id for point in digital_states}
        )
        for tag_id in readable_digital_ids:
            self._digital_readable[tag_id] = True
        for tag_id in bad_digital_ids:
            self._digital_readable[tag_id] = False
        now = asyncio.get_running_loop().time()
        heartbeat_ids = [
            tag_id
            for tag_id in readable_digital_ids
            if now - self._digital_heartbeat.get(tag_id, 0.0)
            >= _DIGITAL_HEARTBEAT_SEC
        ]
        await self._db.touch_digital_states(heartbeat_ids)
        for tag_id in heartbeat_ids:
            self._digital_heartbeat[tag_id] = now
        await self._db.upsert_digital_states(digital_states)
        await self._db.insert_telemetry(telemetry)
        await self._db.insert_digital_telemetry(digital_telemetry)
        await self._db.insert_alarms(alarms)

        if telemetry or digital_states or digital_telemetry or alarms:
            log.debug(
                "ПЛК %s: telemetry=%s digital_states=%s digital_changes=%s alarms=%s",
                client.plc.name,
                len(telemetry),
                len(digital_states),
                len(digital_telemetry),
                len(alarms),
            )

    # --- Корректное завершение фоновых задач сервиса ---
    async def _shutdown_workers(self) -> None:
        for job in self._browse_jobs.values():
            job.cancel()
        self._browse_jobs.clear()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for plc_id in self._clients:
            await self._update_plc_status_safe(
                plc_id,
                is_connected=False,
                last_error="Poller service stopped",
            )
        self._tasks.clear()
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        self._checkers.clear()


# ==============================================================================
# 3. ТОЧКА ВХОДА И ОБРАБОТКА СИГНАЛОВ
# ==============================================================================
async def _async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    db = Database(settings)
    await db.connect()
    service = PollerService(db, settings.config_reload_sec)

    async def browse_handler(request: web.Request) -> web.Response:
        try:
            job_id = request.query.get("job_id")
            if job_id:
                job = service._browse_jobs.get(job_id)
                if job is None:
                    return web.json_response({"detail": "Browse job not found"}, status=404)
                if not job.done():
                    return web.json_response({"status": "running", "job_id": job_id}, status=202)
                try:
                    variables = job.result()
                except Exception as exc:
                    service._browse_jobs.pop(job_id, None)
                    return web.json_response(
                        {"detail": f"OPC UA browse failed: {exc.__class__.__name__}: {exc}"},
                        status=502,
                    )
                service._browse_jobs.pop(job_id, None)
                return web.json_response({"status": "done", "variables": variables})

            plc_id = int(request.match_info["plc_id"])
            job_id = service.start_browse_job(
                plc_id,
                request.query.get("include_system", "false").lower() == "true",
                request.query.get("db"),
                request.query.get("name"),
            )
            return web.json_response({"status": "started", "job_id": job_id}, status=202)
        except LookupError as exc:
            return web.json_response({"detail": str(exc)}, status=404)
        except Exception as exc:
            log.exception("Browse OPC UA завершился ошибкой")
            return web.json_response({"detail": f"OPC UA browse failed: {exc.__class__.__name__}: {exc}"}, status=502)

    async def children_handler(request: web.Request) -> web.Response:
        node_id = request.query.get("node_id")
        if not node_id:
            return web.json_response({"detail": "node_id is required"}, status=400)
        client = service._clients.get(int(request.match_info["plc_id"]))
        if client is None:
            return web.json_response({"detail": "PLC is not active"}, status=404)
        try:
            return web.json_response(await client.browse_children(node_id))
        except Exception as exc:
            log.exception("Чтение дочерних узлов завершилось ошибкой")
            return web.json_response({"detail": f"{exc.__class__.__name__}: {exc}"}, status=502)

    async def node_info_handler(request: web.Request) -> web.Response:
        node_id = request.query.get("node_id")
        if not node_id:
            return web.json_response({"detail": "node_id is required"}, status=400)
        client = service._clients.get(int(request.match_info["plc_id"]))
        if client is None:
            return web.json_response({"detail": "PLC is not active"}, status=404)
        try:
            return web.json_response(await client.read_node_info(node_id))
        except Exception as exc:
            log.exception("Чтение типа узла завершилось ошибкой")
            return web.json_response({"detail": f"{exc.__class__.__name__}: {exc}"}, status=502)

    async def values_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            node_ids = [str(n) for n in body.get("node_ids", [])][:200]
        except Exception:
            return web.json_response({"detail": "Invalid JSON body"}, status=400)
        client = service._clients.get(int(request.match_info["plc_id"]))
        if client is None:
            return web.json_response({"detail": "PLC is not active"}, status=404)
        try:
            return web.json_response({"values": await client.read_values(node_ids)})
        except Exception as exc:
            return web.json_response({"detail": f"{exc.__class__.__name__}: {exc}"}, status=502)

    browse_app = web.Application()
    browse_app.router.add_post("/internal/plcs/{plc_id}/opcua/values", values_handler)
    browse_app.router.add_get("/internal/plcs/{plc_id}/opcua/node-info", node_info_handler)
    browse_app.router.add_get("/internal/plcs/{plc_id}/opcua/variables", browse_handler)
    browse_app.router.add_get("/internal/plcs/{plc_id}/opcua/children", children_handler)
    browse_runner = web.AppRunner(browse_app)
    await browse_runner.setup()
    await web.TCPSite(browse_runner, "0.0.0.0", 8080).start()
    log.info("Внутренний Browse API запущен на порту 8080")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, service.request_stop)

    try:
        await service.run()
    finally:
        await service._shutdown_workers()
        await browse_runner.cleanup()
        await db.close()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()