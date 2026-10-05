"""Справочники простоев: причины, участки и станции (правят администратор и начальники, архивирование вместо удаления)."""
from __future__ import annotations

import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import require_user

SCOPES = {"Chery": "Chery", "Changan": "Changan", "GWM": "GWM", "FL": "Finish Line"}
EDITOR_ROLES = {"admin", "chief", "area_head"}

router = APIRouter(prefix="/api/kpi")


async def ensure_dictionary_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_reasons (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            hint       TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active  BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_areas (
            id         SERIAL PRIMARY KEY,
            scope      TEXT NOT NULL,
            name       TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active  BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (scope, name)
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_stations (
            id         SERIAL PRIMARY KEY,
            area_id    INTEGER NOT NULL REFERENCES kpi_areas(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active  BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (area_id, name)
        )
        """
    )


SEED_REASONS = [
    ("Голодание", ""),
    ("Блокировка", "БДЦ полный или АЕ для ML"),
    ("Оборудование. Работа ОТО", "Устранение неисправностей"),
    ("Ошибка оператора", ""),
    ("Остановка качества", ""),
    ("Качество деталей", ""),
    ("Смена модели", ""),
    ("Потеря скорости", ""),
    ("Отключение питания/Воздуха/Газа/Воды", ""),
    ("Другое", "Описание вводится вручную"),
]


def _rng(prefix: str, start: int, end: int, step: int = 10) -> list[str]:
    return [f"{prefix}-{n}" for n in range(start, end + 1, step)]


# Списки из Excel («Онлайн доски BS»): бренд -> участок -> станции (дубли внутри списка сливаются при заполнении)
SEED_STATIONS: dict[str, dict[str, list[str]]] = {
    "Chery": {
        "Main Line": _rng("UB", 10, 130) + _rng("MB", 140, 300) + ["AE конвейер", "Другое"],
        "Body side": ["WH2 L/R-10", "WH2 L/R-20", "WH2 L/R-10", "OTR L/R-10", "BS L/R - 20", "BS L/R - 40",
                      "OTR L/R -10", "BS L/R -20", "BS L/R-40", "Другое"],
        "Floor subassembly": ["EC2- 10 RH", "EC2- 10 LH", "FF2- 10", "FF2- 20", "FF2- 25", "FF2- 30",
                              "RF2-40", "RF2-30", "RF2-20", "RF2-10", "Другое"],
    },
    "Changan": {
        "Main Line": _rng("UB", 20, 50) + _rng("MB", 60, 190) + ["AE конвейер", "Другое"],
        "Body side": ["BS L/R-10", "BS L/R-20", "BS L/R-30", "HL L/R-10", "WH L/R-10", "WH L/R-20",
                      "WH L/R-30", "WH L/R-40", "Другое"],
        "Floor subassembly": ["EC1-10", "EC1-15", "EC1-20", "EC1-30", "FF1-10", "FF1-20", "FF1-30",
                              "RF1-10", "RF1-20", "RF1-30", "RF1-40", "RF1-50", "RF1-60", "Другое"],
    },
    "GWM": {
        "Main Line": _rng("UB", 20, 100) + _rng("MB", 110, 250) + ["AE конвейер", "Другое"],
        "Body side": _rng("BS L/R", 10, 50) + ["OTR L/R -10", "OTR L/R -20", "INR L/R - 10", "WD L/R- 10"]
                     + _rng("WH L/R", 10, 70) + ["Другое"],
        "Floor subassembly": ["EC2-10", "EC2-15", "EC2-20", "EC2-30", "EC2-40", "EC2-50", "FF1-10", "FF1-20",
                              "RF-10", "RF-20", "RF-30", "RF-40", "RF-50", "RF-60", "DSH1-10", "DSH1-20", "Другое"],
    },
    "FL": {
        "Посты Finish Line": ["Shattle_262", "FL01. EL_263_Lifter"]
                             + [f"FL{n - 262:02d}. RB_{n}" for n in range(264, 280)]
                             + ["RB_280", "Shattle_281", "RB_282"]
                             + [f"FL{n - 261:02d}. RB_{n}" for n in range(283, 293)]
                             + ["RB_293", "Lifter_EL_294", "BDC", "Другое"],
    },
}


def _norm_name(name: str) -> str:
    """Единый вид названия: схлопнутые пробелы, без пробелов вокруг дефиса."""
    return re.sub(r"\s*-\s*", "-", " ".join(name.split()))


def _dedup(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        n = _norm_name(n)
        key = n.casefold()
        if key not in seen:
            seen.add(key)
            out.append(n)
    # «Другое» всегда последней
    return [n for n in out if n != "Другое"] + (["Другое"] if "Другое" in out else [])


async def seed_dictionary(pool: asyncpg.Pool) -> None:
    """Первичное заполнение; выполняется только в пустых таблицах, потом данные правят в интерфейсе."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kpi_reasons)"):
                for i, (name, hint) in enumerate(SEED_REASONS, 1):
                    await conn.execute(
                        "INSERT INTO kpi_reasons (name, hint, sort_order) VALUES ($1, $2, $3)", name, hint, i * 10
                    )
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kpi_areas)"):
                for scope, areas in SEED_STATIONS.items():
                    for ai, (area, stations) in enumerate(areas.items(), 1):
                        area_id = await conn.fetchval(
                            "INSERT INTO kpi_areas (scope, name, sort_order) VALUES ($1, $2, $3) RETURNING id",
                            scope, area, ai * 10,
                        )
                        for si, st in enumerate(_dedup(stations), 1):
                            await conn.execute(
                                "INSERT INTO kpi_stations (area_id, name, sort_order) VALUES ($1, $2, $3)",
                                area_id, st, si * 10,
                            )


async def require_dictionary_editor(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user["role"] not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Справочники могут менять администратор и начальники")
    return user


class ReasonBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    hint: str = Field(default="", max_length=300)
    is_active: bool = True
    sort_order: int | None = Field(default=None, ge=0, le=100000)


class AreaBody(BaseModel):
    scope: str | None = None
    name: str = Field(min_length=1, max_length=120)
    is_active: bool = True
    sort_order: int | None = Field(default=None, ge=0, le=100000)


class StationBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    is_active: bool = True
    sort_order: int | None = Field(default=None, ge=0, le=100000)


@router.get("/dictionary")
async def get_dictionary(request: Request, include_inactive: bool = Query(default=True)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    flt = "" if include_inactive else "WHERE is_active"
    reasons = await pool.fetch(f"SELECT id, name, hint, sort_order, is_active FROM kpi_reasons {flt} ORDER BY sort_order, id")
    areas = await pool.fetch(f"SELECT id, scope, name, sort_order, is_active FROM kpi_areas {flt} ORDER BY scope, sort_order, id")
    stations = await pool.fetch(
        "SELECT id, area_id, name, sort_order, is_active FROM kpi_stations "
        + ("" if include_inactive else "WHERE is_active ")
        + "ORDER BY area_id, sort_order, id"
    )
    by_area: dict[int, list[dict[str, Any]]] = {}
    for s in stations:
        by_area.setdefault(s["area_id"], []).append(dict(s))
    return {
        "scopes": SCOPES,
        "reasons": [dict(r) for r in reasons],
        "areas": [{**dict(a), "stations": by_area.get(a["id"], [])} for a in areas],
    }


async def _next_order(pool: asyncpg.Pool, sql: str, *args: Any) -> int:
    return int(await pool.fetchval(sql, *args) or 0) + 10


@router.post("/reasons", status_code=201)
async def create_reason(body: ReasonBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, int]:
    pool: asyncpg.Pool = request.app.state.pool
    order = body.sort_order if body.sort_order is not None else await _next_order(pool, "SELECT max(sort_order) FROM kpi_reasons")
    try:
        rid = await pool.fetchval(
            "INSERT INTO kpi_reasons (name, hint, sort_order, is_active) VALUES ($1, $2, $3, $4) RETURNING id",
            body.name.strip(), body.hint.strip(), order, body.is_active,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Такая причина уже есть") from exc
    return {"id": rid}


@router.put("/reasons/{reason_id}")
async def update_reason(reason_id: int, body: ReasonBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    try:
        result = await pool.execute(
            "UPDATE kpi_reasons SET name = $2, hint = $3, is_active = $4, sort_order = COALESCE($5, sort_order) WHERE id = $1",
            reason_id, body.name.strip(), body.hint.strip(), body.is_active, body.sort_order,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Причина с таким названием уже есть") from exc
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Причина не найдена")
    return {"status": "ok"}


@router.post("/areas", status_code=201)
async def create_area(body: AreaBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, int]:
    pool: asyncpg.Pool = request.app.state.pool
    if body.scope not in SCOPES:
        raise HTTPException(status_code=422, detail="Неизвестное направление (бренд или Finish Line)")
    order = body.sort_order if body.sort_order is not None else await _next_order(pool, "SELECT max(sort_order) FROM kpi_areas WHERE scope = $1", body.scope)
    try:
        aid = await pool.fetchval(
            "INSERT INTO kpi_areas (scope, name, sort_order, is_active) VALUES ($1, $2, $3, $4) RETURNING id",
            body.scope, body.name.strip(), order, body.is_active,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Такой участок уже есть") from exc
    return {"id": aid}


@router.put("/areas/{area_id}")
async def update_area(area_id: int, body: AreaBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    try:
        result = await pool.execute(
            "UPDATE kpi_areas SET name = $2, is_active = $3, sort_order = COALESCE($4, sort_order) WHERE id = $1",
            area_id, body.name.strip(), body.is_active, body.sort_order,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Участок с таким названием уже есть") from exc
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Участок не найден")
    return {"status": "ok"}


@router.post("/areas/{area_id}/stations", status_code=201)
async def create_station(area_id: int, body: StationBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, int]:
    pool: asyncpg.Pool = request.app.state.pool
    if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM kpi_areas WHERE id = $1)", area_id):
        raise HTTPException(status_code=404, detail="Участок не найден")
    order = body.sort_order if body.sort_order is not None else await _next_order(pool, "SELECT max(sort_order) FROM kpi_stations WHERE area_id = $1", area_id)
    try:
        sid = await pool.fetchval(
            "INSERT INTO kpi_stations (area_id, name, sort_order, is_active) VALUES ($1, $2, $3, $4) RETURNING id",
            area_id, body.name.strip(), order, body.is_active,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Такая станция уже есть в этом участке") from exc
    return {"id": sid}


@router.put("/stations/{station_id}")
async def update_station(station_id: int, body: StationBody, request: Request, _: dict[str, Any] = Depends(require_dictionary_editor)) -> dict[str, str]:
    pool: asyncpg.Pool = request.app.state.pool
    try:
        result = await pool.execute(
            "UPDATE kpi_stations SET name = $2, is_active = $3, sort_order = COALESCE($4, sort_order) WHERE id = $1",
            station_id, body.name.strip(), body.is_active, body.sort_order,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Станция с таким названием уже есть в этом участке") from exc
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Станция не найдена")
    return {"status": "ok"}
