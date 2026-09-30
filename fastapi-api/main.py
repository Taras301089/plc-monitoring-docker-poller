from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://n8n_user:my_strong_password@postgres_db:5432/general_data_hub_BD",
)
STATIC_DIR = Path(__file__).parent / "static"
POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")


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
    name: str = Field(min_length=1, max_length=255)
    node_id: str = Field(min_length=1, max_length=1024)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="PLC Monitoring API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{filename}", response_class=FileResponse)
async def static_file(filename: str) -> FileResponse:
    file_path = STATIC_DIR / filename
    if not file_path.is_file() or not file_path.resolve().is_relative_to(STATIC_DIR.resolve()):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/plcs")
async def list_plcs() -> list[dict[str, Any]]:
    pool = await get_pool(app)
    rows = await pool.fetch(
        """
        SELECT id, name, opc_endpoint, is_active
        FROM plcs
        ORDER BY id
        """
    )
    result = []
    for row in rows:
        d = dict(row)
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


@app.post("/api/plcs")
async def create_plc(request: CreatePlcRequest) -> dict[str, Any]:
    pool = await get_pool(app)
    try:
        plc_id = await pool.fetchval(
            """
            INSERT INTO plcs (name, opc_endpoint, is_active)
            VALUES ($1, $2, TRUE)
            RETURNING id
            """,
            request.name, request.opc_endpoint
        )
        return {"id": plc_id, "name": request.name, "opc_endpoint": request.opc_endpoint, "is_active": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/plcs/{plc_id}")
async def update_plc(plc_id: int, request: CreatePlcRequest) -> dict[str, Any]:
    pool = await get_pool(app)
    try:
        result = await pool.fetchrow(
            """
            UPDATE plcs SET name = $1, opc_endpoint = $2
            WHERE id = $3
            RETURNING id, name, opc_endpoint, is_active
            """,
            request.name, request.opc_endpoint, plc_id
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


@app.delete("/api/plcs/{plc_id}")
async def delete_plc(plc_id: int) -> dict[str, str]:
    pool = await get_pool(app)
    result = await pool.execute("DELETE FROM plcs WHERE id = $1", plc_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="ПЛК не найден")
    return {"status": "ok", "message": "ПЛК удалён"}


@app.get("/api/plcs/{plc_id}/opcua/children")
async def browse_plc_children(plc_id: int, node_id: str = Query(min_length=1)) -> Any:
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
) -> Any:
    pool = await get_pool(app)

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
async def save_plc_tags(plc_id: int, request: SaveTagsRequest) -> dict[str, int]:
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
