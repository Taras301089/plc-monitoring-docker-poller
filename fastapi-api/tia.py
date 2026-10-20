"""Выгрузка проектов TIA Portal в XML: кнопка на сайте, работу выполняет служба tia-bridge.ps1 на Windows.

Контейнер Linux не может вызвать TIA Openness (это Windows-программа), поэтому обмен идёт через общую папку (в контейнере /tia-bridge,
на ПК C:\\tia-bridge): сайт кладёт заявку requests/<id>.json, служба выгружает проект скриптом tia-export.ps1 и пишет jobs/<id>/status.json
и ZIP в out/ (копия ZIP кладётся в папку, которую администратор указал в поле «Путь сохранения»). Всё только для администратора;
пути проектов берутся из tia-projects.json, а не от клиента; путь сохранения проверяет сайт (check_target) и ещё раз служба."""
from __future__ import annotations

import json
import os
import re
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import require_admin
from readonly import READ_ONLY
import tia_logic

router = APIRouter(prefix="/api/tia", tags=["tia"])

BRIDGE_DIR = Path(os.environ.get("TIA_BRIDGE_DIR", "/tia-bridge"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ZIP_RE = re.compile(r"^TIA_[A-Za-z0-9_-]{1,40}_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.zip$")
ALIVE_SEC = 30                      # служба считается работающей, если отметка bridge.json не старше
ACTIVE = ("queued", "running")
DEFAULT_TARGET = r"C:\tia-bridge\out"   # папка out службы на ПК сервера: туда ZIP кладётся всегда
TARGET_MAX = 240
_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')
_SYSTEM_TOP = ("windows", "programdata", "$recycle.bin", "system volume information", "recovery", "boot")


def check_target(raw: str | None) -> str | None:
    r"""Путь сохранения копии ZIP: None, если не задан; иначе нормализованный путь или ValueError с понятной причиной.

    Допустимо: абсолютный путь с буквой диска (C:\...) или UNC (\\сервер\ресурс\...), без «..», не длиннее 240 знаков.
    Запрещено: корень диска, системные папки (Windows, Program Files*, ProgramData и т.п.), сама папка Users и корень профиля
    пользователя (C:\Users\<имя>); вложенные папки профиля (C:\Users\<имя>\Documents\...) разрешены.
    Та же проверка есть в tia-bridge.ps1 (Test-TargetDir): служба проверяет ещё раз и пробует записать в папку."""
    if raw is None:
        return None
    p = str(raw).strip().replace("/", "\\")
    if not p:
        return None
    if len(p) > TARGET_MAX:
        raise ValueError(f"Путь сохранения длиннее {TARGET_MAX} знаков")
    if _BAD_CHARS.search(p):
        raise ValueError('Путь сохранения содержит недопустимые знаки (< > " | ? *)')
    if p.startswith("\\\\"):
        parts = p[2:].split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1] or ":" in p:
            raise ValueError(r"Сетевой путь пишется как \\сервер\папка\...")
        segs, drive = parts[2:], None
    elif re.match(r"^[A-Za-z]:\\", p):
        if ":" in p[2:]:
            raise ValueError("Двоеточие допустимо только после буквы диска")
        segs, drive = p[3:].split("\\"), p[0].upper()
    else:
        raise ValueError(r"Укажите полный путь с буквой диска (C:\папка) или сетевой путь (\\сервер\папка)")
    while segs and segs[-1] == "":
        segs.pop()                                    # завершающая косая черта
    if any(s == "" for s in segs):
        raise ValueError("В пути две косые черты подряд")
    if any(s.strip(" .") == "" for s in segs):
        raise ValueError("Переход «..» и папки из одних точек в пути запрещены")
    if drive:
        if not segs:
            raise ValueError("Корень диска нельзя выбрать: укажите папку")
        top = segs[0].lower()
        if top in _SYSTEM_TOP or top.startswith("program files"):
            raise ValueError(f"Системная папка {drive}:\\{segs[0]} не подходит для сохранения")
        if top == "users" and len(segs) <= 2:
            raise ValueError(r"Нельзя сохранять в папку Users или в корень профиля: укажите вложенную папку (например, Documents\TIA)")
        return f"{drive}:\\" + "\\".join(segs)
    return "\\\\" + "\\".join(parts[:2] + segs)


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
    """Разрешённые проекты из tia-projects.json: id (латиница, цифры, _ -), name, path, plc (необязательно: имя ПЛК в PMM,
    сайт по нему показывает проект в списке «Источник»). Путь проекта клиенту не отдаётся."""
    cfg = _read_json(BRIDGE_DIR / "tia-projects.json")
    items = cfg.get("projects") if isinstance(cfg, dict) else None
    out = []
    for p in items or []:
        if isinstance(p, dict) and ID_RE.match(str(p.get("id", ""))) and p.get("path"):
            out.append({"id": p["id"], "name": str(p.get("name") or p["id"]), "path": str(p["path"]),
                        "plc": str(p.get("plc") or "").strip() or None})
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
            return {k: j.get(k) for k in ("file", "size", "finished_at", "exported", "skipped", "failed",
                                          "texts", "prodiag", "target", "saved_to", "copy_error")}
    return None


def _last_target(jobs: list[dict[str, Any]]) -> str | None:
    """Последний путь сохранения, указанный для проекта (из новой задачи к старой)."""
    for j in jobs:
        if j.get("target"):
            return str(j["target"])
    return None


def _public_job(j: dict[str, Any] | None) -> dict[str, Any] | None:
    if not j:
        return None
    return {k: j.get(k) for k in ("id", "state", "requested_by", "requested_at", "started_at", "finished_at", "error", "target")}


def user_label(user: dict[str, Any]) -> str:
    return f"{user.get('last_name', '')}, {user.get('login', '')}".strip(", ")


@router.get("/projects", summary="Проекты TIA для выгрузки и состояние службы (только администратор)")
async def get_projects(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    rows = []
    for p in load_projects():
        jobs = _jobs_of(p["id"])
        last = _last_done(jobs)
        rows.append({"id": p["id"], "name": p["name"], "plc": p["plc"], "target": _last_target(jobs),
                     "job": _public_job(jobs[0] if jobs else None), "last": last, "logic": _logic_state(p["id"], last)})
    return {"alive": bridge_alive(), "default_target": DEFAULT_TARGET, "projects": rows}


# --- Файл для ИИ: логика ПЛК из последнего ZIP (tia_logic.py), собирает сайт, не служба tia-bridge ---
# Файл лежит в <BRIDGE_DIR>/logic/<id проекта>.json (на ПК C:\tia-bridge\logic\), состояние рядом в <id>.status.json (не в БД).
LOGIC_STALE_SEC = 900           # состояние «создаётся» старше этого считается брошенным (контейнер перезапускали)
_logic_lock = threading.Lock()
_logic_busy: set[str] = set()


def _spawn(fn: Any, *args: Any) -> None:
    """Запуск сборки в фоне, чтобы запрос не ждал (тайм-аут запроса 6 с); в тестах подменяется на синхронный вызов."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _logic_paths(project_id: str) -> tuple[Path, Path]:
    d = BRIDGE_DIR / "logic"
    return d / f"{project_id}.json", d / f"{project_id}.status.json"


def build_logic(project_id: str, zip_name: str) -> None:
    """Собирает файл логики из ZIP выгрузки; старый файл заменяется атомарно (временный файл + replace)."""
    if not ID_RE.match(project_id):
        return                                  # имя файла только из безопасного набора знаков, иначе ничего не пишем
    out, st_path = _logic_paths(project_id)
    started = datetime.now(timezone.utc).isoformat()
    try:
        if not ZIP_RE.match(zip_name):
            raise ValueError("недопустимое имя архива")
        _write_json(st_path, {"state": "building", "started_at": started, "zip": zip_name})
        zpath = BRIDGE_DIR / "out" / zip_name
        if not zpath.is_file():
            raise FileNotFoundError("архив выгрузки не найден (заменён более новой выгрузкой?)")
        try:
            logic = tia_logic.TiaLogic.load(zpath)
        except zipfile.BadZipFile:
            raise ValueError("архив повреждён: это не ZIP") from None
        if not logic.blocks:
            raise ValueError("в архиве не найдено ни одного блока FC/FB/OB")
        data = logic.to_json().encode("utf-8")
        tmp = out.with_name(out.name + ".tmp")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, out)
        _write_json(st_path, {"state": "ready", "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "zip": zip_name,
                              "size": len(data), "blocks": len(logic.blocks), "writes": logic.stats()["writes"]})
    except Exception as ex:      # noqa: BLE001 - любая причина показывается в окне, старый файл остаётся нетронутым
        try:
            _write_json(st_path, {"state": "error", "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                                  "zip": zip_name, "error": (str(ex) or ex.__class__.__name__)[:300]})
        except OSError:
            pass                    # записать состояние нельзя совсем: «создаётся» без живой сборки через LOGIC_STALE_SEC станет ошибкой
    finally:
        with _logic_lock:
            _logic_busy.discard(project_id)


def logic_writable() -> bool:
    """Можно ли писать в <BRIDGE_DIR>/logic: тестовая копия (READ_ONLY=1) и каталог только для чтения (:ro) сборку не запускают."""
    if READ_ONLY:
        return False
    d = BRIDGE_DIR / "logic"
    return os.access(d if d.is_dir() else BRIDGE_DIR, os.W_OK)


def _file_state(out: Path, st: dict[str, Any]) -> dict[str, Any]:
    """Готовый файл в режиме только чтения: дата и размер по самому файлу, число блоков из состояния рядом (если оно есть)."""
    stat = out.stat()
    res: dict[str, Any] = {"state": "ready", "readonly": True, "size": stat.st_size,
                           "finished_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
    for k in ("blocks", "writes", "zip"):
        if st.get(k) is not None:
            res[k] = st[k]
    return res


def _start_logic(project_id: str, zip_name: str) -> bool:
    with _logic_lock:
        if project_id in _logic_busy:
            return False
        _logic_busy.add(project_id)
    _spawn(build_logic, project_id, zip_name)
    return True


def _logic_state(project_id: str, last: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Состояние файла для ИИ; новую сборку запускает сам: появился ZIP новее, чем тот, из которого собран файл."""
    out, st_path = _logic_paths(project_id)
    st = _read_json(st_path)
    st = st if isinstance(st, dict) else {}
    if not logic_writable():
        if out.is_file():
            return _file_state(out, st)
        return {"state": "readonly"}
    if st.get("state") == "building":
        try:
            age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(str(st["started_at"]))).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = LOGIC_STALE_SEC + 1
        with _logic_lock:
            alive = project_id in _logic_busy
        if not alive and age > LOGIC_STALE_SEC:
            st = {"state": "error", "zip": st.get("zip"), "error": "сборка прервана (сайт перезапускали), нажмите «Пересобрать»"}
    elif st.get("state") == "ready" and not out.is_file():
        st = {}
    if last and last.get("file") and st.get("zip") != last["file"] and st.get("state") != "building":
        if _start_logic(project_id, str(last["file"])):
            st = {"state": "building", "zip": last["file"], "started_at": datetime.now(timezone.utc).isoformat()}
    if not st:
        return {"state": "none"}
    return {k: st.get(k) for k in ("state", "finished_at", "size", "blocks", "writes", "error", "zip")}


class ExportBody(BaseModel):
    project_ids: list[str] = Field(min_length=1, max_length=20)
    target_dir: str | None = Field(default=None, max_length=1000)


@router.post("/export", summary="Поставить выгрузку проектов TIA в очередь (только администратор)")
async def start_export(body: ExportBody, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        target = check_target(body.target_dir)
    except ValueError as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from None
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
        st = {"project": pid, "state": "queued", "requested_by": user_label(user), "requested_at": now.isoformat()}
        req = {"id": jid, "project": pid, "requested_by": user_label(user)}
        if target:
            st["target"] = req["target"] = target
        _write_json(BRIDGE_DIR / "jobs" / jid / "status.json", st)
        _write_json(BRIDGE_DIR / "requests" / f"{jid}.json", req)
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


def _known_project(project_id: str) -> dict[str, str]:
    """Имя файла берётся только из списка проектов tia-projects.json, а не от клиента: из папки logic не выйти."""
    if not ID_RE.match(project_id):
        raise HTTPException(status_code=400, detail="Недопустимое имя проекта")
    for p in load_projects():
        if p["id"] == project_id:
            return p
    raise HTTPException(status_code=404, detail="Проект не найден в списке разрешённых")


@router.post("/logic/{project_id}/rebuild", summary="Пересобрать файл для ИИ из последнего ZIP без новой выгрузки из TIA (только администратор)")
async def rebuild_logic(project_id: str, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    _known_project(project_id)
    if not logic_writable():
        raise HTTPException(status_code=409, detail="Недоступно в тестовой копии (только чтение): файл для ИИ собирается на рабочем сервере")
    last = _last_done(_jobs_of(project_id))
    if not last:
        raise HTTPException(status_code=409, detail="Нет готовой выгрузки проекта: сначала выгрузите его из TIA")
    if not _start_logic(project_id, str(last["file"])):
        raise HTTPException(status_code=409, detail="Файл для ИИ уже создаётся")
    return {"started": True, "zip": last["file"]}


@router.get("/logic/{project_id}", summary="Скачать JSON с логикой ПЛК для ИИ (только администратор)")
async def download_logic(project_id: str, _: dict[str, Any] = Depends(require_admin)) -> FileResponse:
    _known_project(project_id)
    path, _st = _logic_paths(project_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл для ИИ ещё не создан")
    return FileResponse(path, media_type="application/json", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('logic_' + project_id + '.json')}"})
