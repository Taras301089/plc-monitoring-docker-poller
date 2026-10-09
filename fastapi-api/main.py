from __future__ import annotations

import asyncio
import json
import re
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import current_user, ensure_auth_schema, require_admin, require_editor, require_user, router as auth_router
from downtime import ensure_downtime_schema, router as downtime_router
from history import router as history_router
from colwidths import ensure_colwidths_schema, router as colwidths_router
from period import router as period_router
from reports import ensure_reports_schema, reports_loop, router as reports_router
from activity import ensure_activity_schema, router as activity_router
from comments import ensure_comments_schema, router as comments_router
from backups import router as backups_router
from trends import ensure_trends_schema, router as trends_router
from manual import ensure_manual_schema, seed_manual_screens, router as manual_router
from dictionary import ensure_dictionary_schema, seed_dictionary, router as dictionary_router

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://n8n_user:my_strong_password@postgres_db:5432/general_data_hub_BD",
)
STATIC_DIR = Path(__file__).parent / "static"
POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")
# То же смещение, что у KPI: контейнер живёт в UTC, производство — часы ПК (UTC+5)
UTC_OFFSET_HOURS = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))


@dataclass(slots=True)
class OpcuaVariable:
    db_name: str
    variable_name: str
    node_id: str
    namespace_index: int
    node_class: str
    data_type: str
    browse_path: str
    is_system: bool


class SelectedTag(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    node_id: str = Field(min_length=1, max_length=255)
    tag_type: str = Field(pattern="^(ANALOG|DIGITAL)$")
    is_archived: bool = False
    is_grafana_plotted: bool = False
    is_alarm_enabled: bool = False


class SaveTagsRequest(BaseModel):
    tags: list[SelectedTag] = Field(default_factory=list)


async def get_pool(app: FastAPI) -> asyncpg.Pool:
    pool = getattr(app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return pool


BODY_COUNTER_ROLES = {
    "tot": "stat_Total_Current_Day",
    "prod": "stat_Hourly_Production",
    "tMin": "stat_Hourly_Takt_Min",
    "tSec": "stat_Hourly_Takt_Sec",
    "starts": "stat_Start_Minutes",
    "idx": "stat_Current_Index",
    "curMin": "stat_Current_Takt_Min",
    "curSec": "stat_Current_Takt_Sec",
    "shMin": "stat_Shift_Takt_Min",
    "shSec": "stat_Shift_Takt_Sec",
}


async def ensure_plc_order(pool: asyncpg.Pool) -> None:
    """Порядок ПЛК в списке (общий для всех): колонка sort_order; ПЛК без номера встают в конец по id."""
    await pool.execute("ALTER TABLE plcs ADD COLUMN IF NOT EXISTS sort_order INTEGER")
    await pool.execute(
        """
        UPDATE plcs p SET sort_order = (SELECT COALESCE(max(sort_order), 0) FROM plcs) + s.rn
        FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn FROM plcs WHERE sort_order IS NULL) s
        WHERE p.id = s.id
        """
    )


async def ensure_kpi_schema(pool: asyncpg.Pool) -> None:
    """Таблицы экранов Andon/KPI; при первом запуске переносит старое расписание (ПЛК + линия) на экраны."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kpi_screens (
                    id         SERIAL PRIMARY KEY,
                    name       TEXT    NOT NULL,
                    plc_id     INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
                    template   TEXT    NOT NULL DEFAULT 'body_counter',
                    db_name    TEXT    NOT NULL DEFAULT '',
                    bindings   JSONB   NOT NULL DEFAULT '{}'::jsonb,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (plc_id, name)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kpi_hourly (
                    screen_id  INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
                    prod_date  DATE    NOT NULL,
                    idx        INTEGER NOT NULL,
                    start_min  INTEGER NOT NULL,
                    end_min    INTEGER,
                    plan       INTEGER NOT NULL DEFAULT 0,
                    fact       INTEGER NOT NULL DEFAULT 0,
                    takt_sec   INTEGER,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (screen_id, prod_date, idx)
                )
                """
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_kpi_hourly_date ON kpi_hourly (prod_date)")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kpi_body_events (
                    id            BIGSERIAL PRIMARY KEY,
                    screen_id     INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    prod_date     DATE NOT NULL,
                    interval_idx  INTEGER,
                    kind          TEXT NOT NULL,
                    model         TEXT,
                    model_now     TEXT,
                    model_age_sec INTEGER,
                    delta         INTEGER NOT NULL DEFAULT 0,
                    total_after   INTEGER
                )
                """
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_kpi_body_events_screen_ts ON kpi_body_events (screen_id, ts)")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kpi_plan_writes (
                    id             BIGSERIAL PRIMARY KEY,
                    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
                    screen_id      INTEGER REFERENCES kpi_screens(id) ON DELETE SET NULL,
                    screen_name    TEXT NOT NULL,
                    plc_id         INTEGER,
                    node_id        TEXT NOT NULL,
                    user_id        INTEGER,
                    user_name      TEXT NOT NULL,
                    old_value      INTEGER,
                    new_value      INTEGER NOT NULL,
                    readback_value INTEGER,
                    ok             BOOLEAN NOT NULL,
                    error          TEXT
                )
                """
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_kpi_plan_writes_screen_ts ON kpi_plan_writes (screen_id, ts DESC)")
            table_exists = await conn.fetchval("SELECT to_regclass('public.kpi_schedule') IS NOT NULL")
            if not table_exists:
                await conn.execute(
                    """
                    CREATE TABLE kpi_schedule (
                        screen_id  INTEGER NOT NULL REFERENCES kpi_screens(id) ON DELETE CASCADE,
                        idx        INTEGER NOT NULL,
                        end_min    INTEGER NOT NULL,
                        plan       INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (screen_id, idx)
                    )
                    """
                )
                return
            has_line = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'kpi_schedule' AND column_name = 'line')"
            )
            if not has_line:
                return
            await conn.execute(
                "ALTER TABLE kpi_schedule ADD COLUMN IF NOT EXISTS screen_id INTEGER "
                "REFERENCES kpi_screens(id) ON DELETE CASCADE"
            )
            pairs = await conn.fetch("SELECT DISTINCT plc_id, line FROM kpi_schedule ORDER BY plc_id, line")
            names = list(BODY_COUNTER_ROLES.values())
            for n, pair in enumerate(pairs):
                plc_id, line = pair["plc_id"], pair["line"]
                db_name = await conn.fetchval(
                    "SELECT db_name FROM plc_discovered_nodes WHERE plc_id = $1 AND node_class = 'Object' "
                    "AND db_name ILIKE $2 ORDER BY db_name LIMIT 1",
                    plc_id,
                    f"{line}\\_counting%hours%",
                )
                bindings: dict[str, str] = {}
                if db_name:
                    rows = await conn.fetch(
                        "SELECT variable_name, node_id FROM plc_discovered_nodes WHERE plc_id = $1 "
                        "AND position($2 in browse_path) > 0 AND node_class = 'Variable' AND variable_name = ANY($3::text[])",
                        plc_id,
                        db_name,
                        names,
                    )
                    by_name = {r["variable_name"]: r["node_id"] for r in rows}
                    bindings = {role: by_name[v] for role, v in BODY_COUNTER_ROLES.items() if v in by_name}
                screen_id = await conn.fetchval(
                    """
                    INSERT INTO kpi_screens (name, plc_id, template, db_name, bindings, sort_order)
                    VALUES ($1, $2, 'body_counter', $3, $4::jsonb, $5)
                    ON CONFLICT (plc_id, name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                    """,
                    line,
                    plc_id,
                    db_name or "",
                    json.dumps(bindings),
                    n,
                )
                await conn.execute(
                    "UPDATE kpi_schedule SET screen_id = $1 WHERE plc_id = $2 AND line = $3",
                    screen_id,
                    plc_id,
                    line,
                )
            await conn.execute("ALTER TABLE kpi_schedule DROP CONSTRAINT IF EXISTS kpi_schedule_pkey")
            await conn.execute("ALTER TABLE kpi_schedule DROP COLUMN plc_id, DROP COLUMN line")
            await conn.execute("ALTER TABLE kpi_schedule ALTER COLUMN screen_id SET NOT NULL")
            await conn.execute("ALTER TABLE kpi_schedule ADD PRIMARY KEY (screen_id, idx)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await ensure_kpi_schema(app.state.pool)
    await ensure_plc_order(app.state.pool)
    await ensure_trends_schema(app.state.pool)
    # Интервал, снятый галочкой в «Плане на день», не показывается на экране Andon (если в нём нет кузовов)
    await app.state.pool.execute("ALTER TABLE kpi_schedule ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE")
    await ensure_auth_schema(app.state.pool)
    await ensure_dictionary_schema(app.state.pool)
    await seed_dictionary(app.state.pool)
    await ensure_downtime_schema(app.state.pool)
    await ensure_manual_schema(app.state.pool)
    await ensure_comments_schema(app.state.pool)
    await ensure_activity_schema(app.state.pool)
    await seed_manual_screens(app.state.pool)
    await ensure_reports_schema(app.state.pool)
    await ensure_colwidths_schema(app.state.pool)
    reports_task = asyncio.create_task(reports_loop(app))
    try:
        yield
    finally:
        reports_task.cancel()
        await app.state.pool.close()


app = FastAPI(title="PLC Monitoring API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(dictionary_router)
app.include_router(downtime_router)
app.include_router(history_router)
app.include_router(colwidths_router)
app.include_router(period_router)
app.include_router(reports_router)
app.include_router(manual_router)
app.include_router(comments_router)
app.include_router(activity_router)
app.include_router(backups_router)
app.include_router(trends_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def no_stale_pages(request, call_next):
    """Страницы и скрипты всегда сверяются с сервером (ETag), чтобы экраны и телевизоры не держали старую версию."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


_ASSET = re.compile(r'(/static/[\w.\-]+\.(?:js|css))"')


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Главная страница: к адресам скриптов и стилей добавляется версия (время изменения файла), чтобы браузеры не держали старые копии."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def versioned(m: re.Match[str]) -> str:
        path = STATIC_DIR / m.group(1).removeprefix("/static/")
        return f'{m.group(1)}?v={int(path.stat().st_mtime) if path.is_file() else 0}"'

    return HTMLResponse(_ASSET.sub(versioned, html))


@app.get("/{filename}", response_class=FileResponse)
async def static_file(filename: str) -> FileResponse:
    file_path = STATIC_DIR / filename
    if not file_path.is_file() or not file_path.resolve().is_relative_to(STATIC_DIR.resolve()):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/time")
async def server_time() -> dict[str, Any]:
    """Текущее время сервиса (ПК / Docker UTC + KPI_UTC_OFFSET_HOURS), без часов браузера."""
    utc = datetime.now(timezone.utc)
    local = utc + timedelta(hours=UTC_OFFSET_HOURS)
    return {
        "unix_ms": int(utc.timestamp() * 1000),
        "offset_hours": UTC_OFFSET_HOURS,
        "date": local.strftime("%d.%m.%Y"),
        "time": local.strftime("%H:%M:%S"),
    }


PLC_STATUS_STALE_SEC = 180   # статус связи старше этого (поллер молчит) не считается подтверждением связи


def plc_link_state(is_active: bool, is_connected: bool | None, status_age: int | None) -> str:
    """ok: связь ЕСТЬ (свежий успешный опрос); no_link: поллер не может подключиться; stale: статус давно не обновлялся
    (поллер не работает); unknown: записи статуса нет; off: ПЛК выключен из опроса."""
    if not is_active:
        return "off"
    if is_connected is None or status_age is None:
        return "unknown"
    if status_age > PLC_STATUS_STALE_SEC:
        return "stale"
    return "ok" if is_connected else "no_link"


_scan_cache: dict[str, Any] = {"at": 0.0, "ages": {}}


async def plc_scan_ages(pool: asyncpg.Pool) -> dict[int, int]:
    """Сколько секунд назад по каждому ПЛК обновляли список переменных («Обновить с ПЛК»); значение кэшируется на 30 с."""
    if time.monotonic() - _scan_cache["at"] > 30:
        rows = await pool.fetch(
            "SELECT plc_id, extract(epoch FROM (now() - max(updated_at)))::int AS age FROM plc_discovered_nodes GROUP BY plc_id"
        )
        _scan_cache["ages"] = {r["plc_id"]: r["age"] for r in rows}
        _scan_cache["at"] = time.monotonic()
    return _scan_cache["ages"]


@app.get("/api/plcs")
async def list_plcs() -> list[dict[str, Any]]:
    pool = await get_pool(app)
    scan_ages = await plc_scan_ages(pool)
    rows = await pool.fetch(
        """
        SELECT p.id, p.name, p.opc_endpoint, p.is_active,
               s.is_connected, COALESCE(NULLIF(s.last_error, ''), '') AS link_error,
               extract(epoch FROM (now() - s.last_successful_poll))::int AS poll_age_sec,
               extract(epoch FROM (now() - s.updated_at))::int AS status_age_sec
        FROM plcs p LEFT JOIN plc_connection_status s ON s.plc_id = p.id
        ORDER BY p.sort_order NULLS LAST, p.id
        """
    )
    result = []
    for row in rows:
        d = dict(row)
        d["link_state"] = plc_link_state(d["is_active"], d.pop("is_connected"), d["status_age_sec"])
        d["scan_age_sec"] = scan_ages.get(d["id"])
        if d['opc_endpoint']:
            import re
            match = re.search(r'://([^:]+)', d['opc_endpoint'])
            d['ip_address'] = match.group(1) if match else 'N/A'
        else:
            d['ip_address'] = 'N/A'
        result.append(d)
    return result


class CreatePlcRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    opc_endpoint: str = Field(min_length=1, max_length=1024)
    is_active: bool | None = None   # «Включён в опрос»: не задан — при создании включён, при правке остаётся как был


@app.post("/api/plcs")
async def create_plc(request: CreatePlcRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool = await get_pool(app)
    try:
        plc_id = await pool.fetchval(
            """
            INSERT INTO plcs (name, opc_endpoint, is_active, sort_order)
            VALUES ($1, $2, $3, (SELECT COALESCE(max(sort_order), 0) + 1 FROM plcs))
            RETURNING id
            """,
            request.name, request.opc_endpoint, True if request.is_active is None else request.is_active
        )
        return {"id": plc_id, "name": request.name, "opc_endpoint": request.opc_endpoint,
                "is_active": True if request.is_active is None else request.is_active}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/plcs/{plc_id}")
async def update_plc(plc_id: int, request: CreatePlcRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool = await get_pool(app)
    try:
        result = await pool.fetchrow(
            """
            UPDATE plcs SET name = $1, opc_endpoint = $2, is_active = COALESCE($4::boolean, is_active)
            WHERE id = $3
            RETURNING id, name, opc_endpoint, is_active
            """,
            request.name, request.opc_endpoint, plc_id, request.is_active
        )
        if not result:
            raise HTTPException(status_code=404, detail="ПЛК не найден")
        d = dict(result)
        if d['opc_endpoint']:
            import re
            match = re.search(r'://([^:]+)', d['opc_endpoint'])
            d['ip_address'] = match.group(1) if match else 'N/A'
        return d
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class MovePlcRequest(BaseModel):
    direction: Literal["up", "down"]


@app.put("/api/plcs/{plc_id}/move")
async def move_plc(plc_id: int, body: MovePlcRequest, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Сдвинуть ПЛК на одну позицию выше или ниже в общем списке (на краю списка ничего не меняется)."""
    pool = await get_pool(app)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("LOCK TABLE plcs IN SHARE ROW EXCLUSIVE MODE")
            ids = [r["id"] for r in await conn.fetch("SELECT id FROM plcs ORDER BY sort_order NULLS LAST, id")]
            if plc_id not in ids:
                raise HTTPException(status_code=404, detail="ПЛК не найден")
            i = ids.index(plc_id)
            j = i - 1 if body.direction == "up" else i + 1
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
            await conn.executemany("UPDATE plcs SET sort_order = $2 WHERE id = $1", [(pid, n + 1) for n, pid in enumerate(ids)])
    return {"order": ids}


@app.delete("/api/plcs/{plc_id}")
async def delete_plc(plc_id: int, _: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    pool = await get_pool(app)
    result = await pool.execute("DELETE FROM plcs WHERE id = $1", plc_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    return {"status": "ok", "message": "ПЛК удалён"}


@app.get("/api/plcs/{plc_id}/opcua/meta")
async def plc_scan_meta(plc_id: int, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool = await get_pool(app)
    row = await pool.fetchrow(
        """
        SELECT max(updated_at) AS last_scan_at, count(*) AS total
        FROM plc_discovered_nodes
        WHERE plc_id = $1 AND is_system = FALSE
        """,
        plc_id,
    )
    ts = row["last_scan_at"]
    return {"last_scan_at": ts.isoformat() if ts else None, "total": row["total"]}


class KpiItem(BaseModel):
    idx: int = Field(ge=1, le=48)
    end_min: int = Field(ge=0, le=2880)
    plan: int = Field(ge=0, le=100000)
    active: bool | None = None      # None — оставить как было (старое окно «Смена и план» этот признак не передаёт)


class KpiScheduleRequest(BaseModel):
    line: str = Field(min_length=1, max_length=100)
    items: list[KpiItem] = Field(default_factory=list, max_length=48)


class KpiScreenBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    plc_id: int
    template: str = Field(default="body_counter", pattern="^[a-z_]{1,40}$")
    db_name: str = Field(default="", max_length=300)
    bindings: dict[str, str] = Field(default_factory=dict, max_length=60)
    sort_order: int = Field(default=0, ge=0, le=10000)


def _screen_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    b = d.get("bindings")
    d["bindings"] = json.loads(b) if isinstance(b, str) else (b or {})
    d.pop("created_at", None)
    return d


_SCREEN_SELECT = (
    "SELECT s.id, s.name, s.plc_id, p.name AS plc_name, s.template, s.db_name, s.bindings, s.sort_order "
    "FROM kpi_screens s JOIN plcs p ON p.id = s.plc_id"
)


@app.get("/api/kpi/screens")
async def list_kpi_screens() -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(f"{_SCREEN_SELECT} ORDER BY s.sort_order, s.id")
    return [_screen_dict(r) for r in rows]


async def _write_screen(pool: asyncpg.Pool, body: KpiScreenBody, screen_id: int | None) -> dict[str, Any]:
    if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM plcs WHERE id = $1)", body.plc_id):
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    try:
        if screen_id is None:
            new_id = await pool.fetchval(
                "INSERT INTO kpi_screens (name, plc_id, template, db_name, bindings, sort_order) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id",
                body.name, body.plc_id, body.template, body.db_name, json.dumps(body.bindings), body.sort_order,
            )
        else:
            new_id = await pool.fetchval(
                "UPDATE kpi_screens SET name = $2, plc_id = $3, template = $4, db_name = $5, "
                "bindings = $6::jsonb, sort_order = $7 WHERE id = $1 RETURNING id",
                screen_id, body.name, body.plc_id, body.template, body.db_name, json.dumps(body.bindings), body.sort_order,
            )
            if new_id is None:
                raise HTTPException(status_code=404, detail="Экран не найден")
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Экран с таким названием на этом ПЛК уже есть") from exc
    row = await pool.fetchrow(f"{_SCREEN_SELECT} WHERE s.id = $1", new_id)
    return _screen_dict(row)


@app.post("/api/kpi/screens", status_code=201)
async def create_kpi_screen(body: KpiScreenBody, _: dict[str, Any] = Depends(require_editor)) -> dict[str, Any]:
    return await _write_screen(await get_pool(app), body, None)


@app.put("/api/kpi/screens/{screen_id}")
async def update_kpi_screen(
    screen_id: int, body: KpiScreenBody, _: dict[str, Any] = Depends(require_editor)
) -> dict[str, Any]:
    return await _write_screen(await get_pool(app), body, screen_id)


@app.delete("/api/kpi/screens/{screen_id}")
async def delete_kpi_screen(screen_id: int, _: dict[str, Any] = Depends(require_editor)) -> dict[str, str]:
    pool = await get_pool(app)
    result = await pool.execute("DELETE FROM kpi_screens WHERE id = $1", screen_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Экран не найден")
    return {"status": "ok"}


class KpiScreenScheduleRequest(BaseModel):
    items: list[KpiItem] = Field(default_factory=list, max_length=48)


@app.get("/api/kpi/screens/{screen_id}/schedule")
async def get_screen_schedule(screen_id: int) -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(
        "SELECT idx, end_min, plan, active FROM kpi_schedule WHERE screen_id = $1 ORDER BY idx", screen_id
    )
    return [dict(r) for r in rows]


@app.put("/api/kpi/screens/{screen_id}/schedule")
async def put_screen_schedule(
    screen_id: int, request: KpiScreenScheduleRequest, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, int]:
    pool = await get_pool(app)
    screen = await pool.fetchrow("SELECT id, name, plc_id FROM kpi_screens WHERE id = $1", screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    new_total = sum(it.plan for it in request.items)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_total = await conn.fetchval("SELECT COALESCE(sum(plan), 0) FROM kpi_schedule WHERE screen_id = $1", screen_id)
            old_active = {r["idx"]: r["active"] for r in await conn.fetch("SELECT idx, active FROM kpi_schedule WHERE screen_id = $1", screen_id)}
            await conn.execute("DELETE FROM kpi_schedule WHERE screen_id = $1", screen_id)
            await conn.executemany(
                "INSERT INTO kpi_schedule (screen_id, idx, end_min, plan, active) VALUES ($1, $2, $3, $4, $5)",
                [(screen_id, it.idx, it.end_min, it.plan, it.active if it.active is not None else old_active.get(it.idx, True))
                 for it in request.items],
            )
            # План на день у линий брендов хранится на сайте: изменение итога пишем в тот же журнал, что и запись плана в ПЛК
            if old_total != new_total:
                await conn.execute(
                    "INSERT INTO kpi_plan_writes (screen_id, screen_name, plc_id, node_id, user_id, user_name, "
                    "old_value, new_value, readback_value, ok) VALUES ($1, $2, $3, 'site:kpi_schedule', $4, $5, $6, $7, $7, TRUE)",
                    screen_id, screen["name"], screen["plc_id"], user["id"], user["full_name"], old_total, new_total,
                )
    return {"saved": len(request.items)}


# Совместимость со старым интерфейсом (ПЛК + название линии) до перехода вкладки Andon на экраны
@app.get("/api/plcs/{plc_id}/kpi/schedule")
async def get_kpi_schedule(plc_id: int, line: str = Query(min_length=1)) -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(
        "SELECT k.idx, k.end_min, k.plan FROM kpi_schedule k JOIN kpi_screens s ON s.id = k.screen_id "
        "WHERE s.plc_id = $1 AND s.name = $2 ORDER BY k.idx",
        plc_id,
        line,
    )
    return [dict(r) for r in rows]


@app.put("/api/plcs/{plc_id}/kpi/schedule")
async def put_kpi_schedule(
    plc_id: int, request: KpiScheduleRequest, user: dict[str, Any] = Depends(require_editor)
) -> dict[str, int]:
    pool = await get_pool(app)
    screen_id = await pool.fetchval(
        "SELECT id FROM kpi_screens WHERE plc_id = $1 AND name = $2", plc_id, request.line
    )
    if screen_id is None:
        raise HTTPException(status_code=404, detail="Экран не найден: сначала создайте его")
    return await put_screen_schedule(screen_id, KpiScreenScheduleRequest(items=request.items), user)


def app_env() -> str:
    """Среда запуска: prod только при APP_ENV=prod, любое другое значение (и пустое) считается разработкой (запись в ПЛК запрещена)."""
    return "prod" if os.getenv("APP_ENV", "dev").strip().lower() == "prod" else "dev"


@app.get("/api/env")
async def get_env() -> dict[str, str]:
    return {"env": app_env()}


class PlanDayBody(BaseModel):
    value: int = Field(ge=0, le=500)


@app.post("/api/kpi/screens/{screen_id}/plan-day")
async def write_plan_day(screen_id: int, body: PlanDayBody, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Запись плана на день в ПЛК: только вошедшие пользователи (кроме роли «Просмотр»), каждая запись попадает в журнал."""
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="У вашей роли нет права менять план")
    pool = await get_pool(app)
    screen = await pool.fetchrow(
        "SELECT id, name, plc_id, template, bindings FROM kpi_screens WHERE id = $1", screen_id
    )
    if screen is None or screen["template"] != "fl_counter":
        raise HTTPException(status_code=404, detail="Экран не найден или не поддерживает запись плана")
    raw = screen["bindings"]
    node_id = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})).get("plan_day", "")
    ok, error, result = False, None, {}
    if app_env() != "prod":
        error = "Режим разработки: запись в ПЛК отключена"
        await pool.execute(
            "INSERT INTO kpi_plan_writes (screen_id, screen_name, plc_id, node_id, user_id, user_name, "
            "old_value, new_value, readback_value, ok, error) VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, NULL, FALSE, $8)",
            screen_id, screen["name"], screen["plc_id"], node_id, user["id"], user["full_name"], body.value, error,
        )
        raise HTTPException(status_code=403, detail=error)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.post(
                f"{POLLER_URL}/internal/kpi/screens/{screen_id}/plan-day", json={"value": body.value}
            )
        result = response.json()
        if response.status_code >= 400:
            error = result.get("detail", "Ошибка записи")
        elif result.get("readback") != body.value:
            error = f"ПЛК вернул другое значение: {result.get('readback')}"
        else:
            ok = True
    except httpx.HTTPError as exc:
        error = f"Поллер недоступен: {exc}"
    await pool.execute(
        "INSERT INTO kpi_plan_writes (screen_id, screen_name, plc_id, node_id, user_id, user_name, "
        "old_value, new_value, readback_value, ok, error) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        screen_id, screen["name"], screen["plc_id"], node_id, user["id"], user["full_name"],
        result.get("old") if isinstance(result.get("old"), int) else None,
        body.value,
        result.get("readback") if isinstance(result.get("readback"), int) else None,
        ok, error,
    )
    if not ok:
        raise HTTPException(status_code=502, detail=error or "Не удалось записать план")
    return {"old": result.get("old"), "new": body.value, "readback": result.get("readback"), "interval_plan": result.get("interval_plan")}


@app.get("/api/kpi/screens/{screen_id}/plan-writes")
async def list_plan_writes(screen_id: int, limit: int = Query(default=20, ge=1, le=200)) -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(
        "SELECT ts, user_name, old_value, new_value, readback_value, ok, error FROM kpi_plan_writes "
        "WHERE screen_id = $1 ORDER BY ts DESC LIMIT $2",
        screen_id, limit,
    )
    return [{**dict(r), "ts": r["ts"].isoformat()} for r in rows]


class ReadValuesRequest(BaseModel):
    node_ids: list[str] = Field(default_factory=list, max_length=200)


@app.post("/api/plcs/{plc_id}/opcua/values")
async def plc_read_values(plc_id: int, request: ReadValuesRequest) -> Any:
    url = f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/values"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            response = await client.post(url, json={"node_ids": request.node_ids})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Poller unavailable: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", "Poller failed"))
    return response.json()


@app.get("/api/plcs/{plc_id}/opcua/node-info")
async def plc_node_info(plc_id: int, node_id: str = Query(min_length=1), _: dict[str, Any] = Depends(require_admin)) -> Any:
    url = f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/node-info"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            response = await client.get(url, params={"node_id": node_id})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Poller unavailable: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", "Poller failed"))
    return response.json()


@app.get("/api/plcs/{plc_id}/tags")
async def list_plc_tags(plc_id: int, _: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(
        """
        SELECT name, node_id, tag_type, is_archived, is_grafana_plotted, is_alarm_enabled
        FROM plc_tags
        WHERE plc_id = $1 AND is_active = TRUE
        """,
        plc_id,
    )
    return [dict(row) for row in rows]


@app.delete("/api/plcs/{plc_id}/tags")
async def deactivate_plc_tag(
    plc_id: int, node_id: str = Query(min_length=1), _: dict[str, Any] = Depends(require_admin)
) -> dict[str, str]:
    pool = await get_pool(app)
    await pool.execute(
        "UPDATE plc_tags SET is_active = FALSE WHERE plc_id = $1 AND node_id = $2",
        plc_id,
        node_id,
    )
    return {"status": "ok"}


@app.get("/api/plcs/{plc_id}/opcua/children")
async def browse_plc_children(plc_id: int, node_id: str = Query(min_length=1), _: dict[str, Any] = Depends(require_admin)) -> Any:
    url = f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/children"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.get(url, params={"node_id": node_id})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Poller unavailable: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", "Poller failed"))
    return response.json()


@app.get("/api/plcs/{plc_id}/opcua/variables")
async def browse_plc_variables(
    plc_id: int,
    db: str | None = Query(default=None),
    name: str | None = Query(default=None),
    include_system: bool = Query(default=False),
    refresh: bool = Query(default=False),
    user: dict[str, Any] = Depends(require_admin),
) -> Any:
    pool = await get_pool(app)
    # Сканирование ПЛК (тяжёлая нагрузка на OPC UA) доступно только администратору; остальные читают кэш из БД
    is_admin = bool(user and user["role"] == "admin")
    if refresh and not is_admin:
        raise HTTPException(
            status_code=401 if user is None else 403,
            detail="Обновлять список с ПЛК может только администратор",
        )

    # Если пользователь НЕ просит принудительное пересканирование (refresh=True)
    if not refresh:
        sql = """
            SELECT db_name, variable_name, node_id, namespace_index,
                   node_class, data_type, browse_path, is_system
            FROM plc_discovered_nodes
            WHERE plc_id = $1
        """
        conditions = []
        args: list[Any] = [plc_id]
        if not include_system:
            conditions.append("is_system = FALSE")
        if db:
            args.append(f"%{db}%")
            conditions.append(f"(db_name ILIKE ${len(args)} OR browse_path ILIKE ${len(args)})")
        if name:
            args.append(f"%{name}%")
            conditions.append(f"variable_name ILIKE ${len(args)}")

        if conditions:
            sql += " AND " + " AND ".join(conditions)
        sql += " ORDER BY id"

        cached_rows = await pool.fetch(sql, *args)
        if cached_rows:
            return [dict(row) for row in cached_rows]
        if not is_admin:
            return []

    # Если в базе нет данных или запрошен refresh=True — запрашиваем у опросчика (Poller)
    params = {"include_system": str(include_system).lower()}
    if db:
        params["db"] = db
    if name:
        params["name"] = name
    url = f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/variables"
    variables: list[dict[str, Any]] | None = None
    try:
        limits = httpx.Timeout(timeout=60.0, connect=10.0)
        async with httpx.AsyncClient(timeout=limits) as client:
            response = await client.get(url, params=params)
            if response.status_code == 202:
                payload = response.json()
                job_id = payload["job_id"]
                for _ in range(600):
                    await asyncio.sleep(1)
                    job_response = await client.get(
                        url, params={"job_id": job_id}
                    )
                    if job_response.status_code == 202:
                        job_payload = job_response.json()
                        if job_payload.get("status") == "done":
                            variables = job_payload.get("variables", [])
                            break
                        continue
                    response = job_response
                    break
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Poller Browse API unavailable: {exc}") from exc
    if variables is None:
        if response.status_code >= 400:
            detail = response.json().get("detail", "Poller Browse failed")
            raise HTTPException(status_code=response.status_code, detail=detail)
        payload = response.json()
        variables = payload.get("variables", []) if isinstance(payload, dict) else payload

    # Сохраняем полученные переменные в БД для кэширования (пакетная вставка)
    if variables and not db and not name:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM plc_discovered_nodes WHERE plc_id = $1", plc_id)
                    # Пакетная вставка вместо цикла для производительности
                    rows = [
                        (
                            plc_id,
                            var.get("db_name", ""),
                            var.get("variable_name", ""),
                            var.get("node_id", ""),
                            var.get("namespace_index", 0),
                            var.get("node_class", ""),
                            var.get("data_type", ""),
                            var.get("browse_path", ""),
                            var.get("is_system", False),
                        )
                        for var in variables
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO plc_discovered_nodes
                            (plc_id, db_name, variable_name, node_id, namespace_index,
                             node_class, data_type, browse_path, is_system)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (plc_id, node_id) DO NOTHING
                        """,
                        rows
                    )
        except Exception as e:
            import traceback
            print(f"Warning: Failed to cache variables: {e}")
            traceback.print_exc()

    return variables



@app.post("/api/plcs/{plc_id}/tags")
async def save_plc_tags(
    plc_id: int, request: SaveTagsRequest, _: dict[str, Any] = Depends(require_admin)
) -> dict[str, int]:
    pool = await get_pool(app)
    plc_exists = await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM plcs WHERE id = $1)", plc_id
    )
    if not plc_exists:
        raise HTTPException(status_code=404, detail="PLC not found")

    async with pool.acquire() as connection:
        async with connection.transaction():
            for tag in request.tags:
                await connection.execute(
                    """
                    INSERT INTO plc_tags
                        (plc_id, name, node_id, tag_type, is_alarm_enabled,
                         is_archived, is_grafana_plotted, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                    ON CONFLICT (plc_id, node_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        tag_type = EXCLUDED.tag_type,
                        is_alarm_enabled = EXCLUDED.is_alarm_enabled,
                        is_archived = EXCLUDED.is_archived,
                        is_grafana_plotted = EXCLUDED.is_grafana_plotted,
                        is_active = TRUE
                    """,
                    plc_id,
                    tag.name,
                    tag.node_id,
                    tag.tag_type,
                    tag.is_alarm_enabled,
                    tag.is_archived,
                    tag.is_grafana_plotted,
                )
    return {"saved": len(request.tags)}
