"""Кнопка «Выгрузить проект TIA»: заявка в общую папку, статус, скачивание ZIP (tia.py) и служба tia-bridge.ps1.

Саму выгрузку делает Windows-служба; здесь проверяется сайт: права, защита имён и путей, очередь, состояние. База не нужна."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import tia

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
ZIP = "TIA_wbs1_2026-10-15_13-05.zip"


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    (tmp_path / "out").mkdir()
    return tmp_path


@pytest.fixture
def admin_client(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"last_name": "Исимов", "login": "isimov", "role": "admin"}
    return TestClient(app)


@pytest.fixture
def anon_client(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    return TestClient(app)


def _alive(bridge, ago=0):
    (bridge / "bridge.json").write_text(json.dumps({"time": (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat()}), encoding="utf-8")


def _projects(bridge, items=None):
    items = items or [{"id": "wbs1", "name": "AE · WBS1", "path": "C:/TIA/WBS1.ap17"}]
    (bridge / "tia-projects.json").write_text(json.dumps({"projects": items}, ensure_ascii=False), encoding="utf-8")


def test_everything_requires_admin(anon_client):
    assert anon_client.get("/api/tia/projects").status_code == 401
    assert anon_client.post("/api/tia/export", json={"project_ids": ["wbs1"]}).status_code == 401
    assert anon_client.get(f"/api/tia/download/{ZIP}").status_code == 401


def test_projects_list_hides_paths_and_bad_ids(admin_client, bridge):
    _alive(bridge)
    _projects(bridge, [{"id": "wbs1", "name": "AE · WBS1", "path": "C:/TIA/WBS1.ap17"}, {"id": "bad id!", "name": "x", "path": "p"}, {"id": "nopath", "name": "x"}])
    r = admin_client.get("/api/tia/projects").json()
    assert r["alive"] is True and [p["id"] for p in r["projects"]] == ["wbs1"]
    assert "path" not in r["projects"][0]


def test_export_needs_running_service_and_known_project(admin_client, bridge):
    _projects(bridge)
    assert admin_client.post("/api/tia/export", json={"project_ids": ["wbs1"]}).status_code == 503      # службы нет
    _alive(bridge, ago=120)
    assert admin_client.get("/api/tia/projects").json()["alive"] is False                                  # отметка устарела
    _alive(bridge)
    assert admin_client.post("/api/tia/export", json={"project_ids": ["unknown"]}).status_code == 404     # путь не от клиента
    assert admin_client.post("/api/tia/export", json={"project_ids": []}).status_code == 422


def test_export_queues_once_and_reports_state(admin_client, bridge):
    _alive(bridge)
    _projects(bridge)
    r = admin_client.post("/api/tia/export", json={"project_ids": ["wbs1", "wbs1"]})
    assert r.status_code == 200 and len(r.json()["started"]) == 1
    jid = r.json()["started"][0]
    req = json.loads((bridge / "requests" / f"{jid}.json").read_text(encoding="utf-8"))
    assert req["project"] == "wbs1" and "path" not in req and req["requested_by"].startswith("Исимов")
    assert admin_client.post("/api/tia/export", json={"project_ids": ["wbs1"]}).status_code == 409        # уже в очереди
    job = admin_client.get("/api/tia/projects").json()["projects"][0]["job"]
    assert job["state"] == "queued" and job["id"] == jid
    # служба закончила: готово, ZIP скачивается, после этого можно выгружать снова
    st = json.loads((bridge / "jobs" / jid / "status.json").read_text(encoding="utf-8"))
    st.update(state="done", finished_at=_now(), file=ZIP, size=502, exported=10, skipped=2, failed=0)
    (bridge / "jobs" / jid / "status.json").write_text(json.dumps(st), encoding="utf-8")
    (bridge / "out" / ZIP).write_bytes(b"PK" + b"0" * 500)
    p = admin_client.get("/api/tia/projects").json()["projects"][0]
    assert p["job"]["state"] == "done" and p["last"]["file"] == ZIP and p["last"]["exported"] == 10 and p["last"]["skipped"] == 2
    d = admin_client.get(f"/api/tia/download/{ZIP}")
    assert d.status_code == 200 and d.content[:2] == b"PK" and f"filename*=UTF-8''{ZIP}" in d.headers["content-disposition"]


def test_download_rejects_bad_names(admin_client, bridge):
    (bridge / "bridge.json").write_text("{}", encoding="utf-8")
    for bad in ("..%2Fbridge.json", "x.zip", ZIP + ".exe", "TIA__2026-10-15_13-05.zip", "TIA_wbs1_2026-10-15_13-05.zip%00.txt"):
        assert admin_client.get("/api/tia/download/" + bad).status_code in (400, 404), bad
    assert admin_client.get("/api/tia/download/TIA_wbs1_2026-10-16_00-00.zip").status_code == 404


def test_screen_and_service_are_wired():
    js = (STATIC / "tiaexport.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/tiaexport.js" in html and "/static/tia.css" in html
    assert "isAdmin()" in js and "tia-all" in js and "Выгрузить выбранные" in js        # общая таблица с «выбрать все», одна кнопка
    for el in ("tia-all", "tia-go"):
        assert re.search(rf'id="{el}"[^>]*title="[^"]{{15,}}"', js), el
    assert "app.include_router(tia_router)" in (ROOT / "fastapi-api" / "main.py").read_text(encoding="utf-8")
    assert "/tia-bridge" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ps = (ROOT / "tia-bridge.ps1").read_text(encoding="utf-8-sig")
    # путь проекта берётся только из tia-projects.json, а не из заявки; без сохранения и записи в проект (это делает tia-export.ps1)
    assert "$req.path" not in ps and "tia-projects.json" in ps and "$req.project" in ps
