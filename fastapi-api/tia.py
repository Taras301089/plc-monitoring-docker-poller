"""Выгрузка проектов TIA Portal в XML: кнопка на сайте, работу выполняет служба tia-bridge.ps1 на Windows.

Контейнер Linux не может вызвать TIA Openness (это Windows-программа), поэтому обмен идёт через общую папку (в контейнере /tia-bridge,
на ПК C:\\tia-bridge): сайт кладёт заявку requests/<id>.json, служба выгружает проект скриптом tia-export.ps1 и пишет jobs/<id>/status.json
и ZIP в out/. Всё только для администратора; пути проектов берутся из tia-projects.json, а не от клиента."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import require_admin

router = APIRouter(prefix="/api/tia", tags=["tia"])

BRIDGE_DIR = Path(os.environ.get("TIA_BRIDGE_DIR", "/tia-bridge"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ZIP_RE = re.compile(r"^TIA_[A-Za-z0-9_-]{1,40}_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.zip$")
ALIVE_SEC = 30                      # служба считается работающей, если отметка bridge.json не старше
ACTIVE = ("queued", "running")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj: Any) -> None:
    """Запись целиком (временный файл, затем замена): служба не прочитает недописанный файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_projects() -> list[dict[str, str]]:
    """Разрешённые проекты из tia-projects.json: id (латиница, цифры, _ -), name, path. Путь клиенту не отдаётся."""
    cfg = _read_json(BRIDGE_DIR / "tia-projects.json")
    items = cfg.get("projects") if isinstance(cfg, dict) else None
    out = []
    for p in items or []:
        if isinstance(p, dict) and ID_RE.match(str(p.get("id", ""))) and p.get("path"):
            out.append({"id": p["id"], "name": str(p.get("name") or p["id"]), "path": str(p["path"])})
    return out


def bridge_alive(now: datetime | None = None) -> bool:
    hb = _read_json(BRIDGE_DIR / "bridge.json")
    try:
        t = datetime.fromisoformat(str(hb["time"]).replace("Z", "+00:00"))
    except (TypeError, KeyError, ValueError):
        return False
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() <= ALIVE_SEC


def _jobs_of(project_id: str) -> list[dict[str, Any]]:
    """Задачи проекта, новые первыми."""
    out = []
    jobs = BRIDGE_DIR / "jobs"
    if jobs.is_dir():
        for d in jobs.iterdir():
            if d.is_dir() and d.name.startswith(project_id + "-"):
                st = _read_json(d / "status.json")
                if isinstance(st, dict) and st.get("project") == project_id:
                    out.append({**st, "id": d.name})
    out.sort(key=lambda s: str(s.get("requested_at", "")), reverse=True)
    return out


def _last_done(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for j in jobs:
        f = j.get("file")
        if j.get("state") == "done" and f and ZIP_RE.match(str(f)) and (BRIDGE_DIR / "out" / f).is_file():
            return {k: j.get(k) for k in ("file", "size", "finished_at", "exported", "skipped", "failed")}
    return None


def _public_job(j: dict[str, Any] | None) -> dict[str, Any] | None:
    if not j:
        return None
    return {k: j.get(k) for k in ("id", "state", "requested_by", "requested_at", "started_at", "finished_at", "error")}


def user_label(user: dict[str, Any]) -> str:
    return f"{user.get('last_name', '')}, {user.get('login', '')}".strip(", ")


@router.get("/projects", summary="Проекты TIA для выгрузки и состояние службы (только администратор)")
async def get_projects(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    rows = []
    for p in load_projects():
        jobs = _jobs_of(p["id"])
        rows.append({"id": p["id"], "name": p["name"], "job": _public_job(jobs[0] if jobs else None), "last": _last_done(jobs)})
    return {"alive": bridge_alive(), "projects": rows}


class ExportBody(BaseModel):
    project_ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/export", summary="Поставить выгрузку проектов TIA в очередь (только администратор)")
async def start_export(body: ExportBody, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if not bridge_alive():
        raise HTTPException(status_code=503, detail="Служба выгрузки TIA не запущена на ПК сервера (задача «PMM TIA bridge»)")
    known = {p["id"] for p in load_projects()}
    wanted = list(dict.fromkeys(body.project_ids))
    bad = [p for p in wanted if p not in known]
    if bad:
        raise HTTPException(status_code=404, detail="Проект не найден в списке разрешённых: " + ", ".join(bad))
    started, skipped = [], []
    now = datetime.now(timezone.utc)
    for pid in wanted:
        jobs = _jobs_of(pid)
        if jobs and jobs[0].get("state") in ACTIVE:
            skipped.append(pid)             # уже в очереди или выгружается: второй раз не ставим
            continue
        jid = f"{pid}-{now.strftime('%Y%m%d-%H%M%S')}"
        _write_json(BRIDGE_DIR / "jobs" / jid / "status.json",
                    {"project": pid, "state": "queued", "requested_by": user_label(user), "requested_at": now.isoformat()})
        _write_json(BRIDGE_DIR / "requests" / f"{jid}.json", {"id": jid, "project": pid, "requested_by": user_label(user)})
        started.append(jid)
    if not started:
        raise HTTPException(status_code=409, detail="Выбранные проекты уже выгружаются")
    return {"started": started, "skipped": skipped}


@router.get("/download/{name}", summary="Скачать ZIP с XML выгруженного проекта (только администратор)")
async def download(name: str, _: dict[str, Any] = Depends(require_admin)) -> FileResponse:
    if not ZIP_RE.match(name):
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    path = BRIDGE_DIR / "out" / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден (возможно, заменён более новой выгрузкой)")
    return FileResponse(path, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
