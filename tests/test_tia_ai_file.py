"""«Файл для ИИ» в окне выгрузки TIA: сайт сам собирает логику ПЛК из ZIP (tia.py + tia_logic.py), состояние рядом с файлом, не в БД.

Проверяется: права, сборка и скачивание, автозапуск после новой выгрузки, атомарная замена, битый ZIP, защита имени проекта,
чистая логика колонки в окне (через node). База не нужна."""
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import tia

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
FIX = Path(__file__).resolve().parent / "fixtures" / "tia_logic" / "PLC1" / "Blocks"
ZIP = "TIA_wbs1_2026-10-19_16-10.zip"
NEW_ZIP = "TIA_wbs1_2026-10-19_17-00.zip"
NODE = shutil.which("node")


def _now():
    return datetime.now(timezone.utc).isoformat()


def make_zip(path: Path, bad: bool = False) -> None:
    if bad:
        path.write_bytes("это не zip".encode("utf-8"))
        return
    with zipfile.ZipFile(path, "w") as z:
        for f in FIX.glob("*.xml"):
            z.write(f, f"2026-10-19_16-10/PLC1/Blocks/{f.name}")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(tia, "_spawn", lambda fn, *a: fn(*a))          # сборка в тесте идёт сразу, без потока
    (tmp_path / "out").mkdir()
    (tmp_path / "tia-projects.json").write_text(json.dumps({"projects": [
        {"id": "wbs1", "name": "AE · WBS1", "path": "C:/TIA/WBS1.ap17", "plc": "AE_WBS_1"},
        {"id": "../evil", "name": "плохой", "path": "C:/x"}]}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def done_job(bridge, zip_name=ZIP, jid="wbs1-20251019-161000", at="2026-10-19T11:10:00+00:00"):
    d = bridge / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"project": "wbs1", "state": "done", "requested_at": at, "finished_at": at, "file": zip_name,
                                               "size": 100}), encoding="utf-8")


@pytest.fixture
def admin(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"last_name": "Исимов", "login": "isimov", "role": "admin"}
    return TestClient(app)


@pytest.fixture
def anon(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    return TestClient(app)


def logic_of(client):
    return client.get("/api/tia/projects").json()["projects"][0]["logic"]


def test_non_admin_is_refused(anon, bridge):
    assert anon.post("/api/tia/logic/wbs1/rebuild").status_code == 401
    assert anon.get("/api/tia/logic/wbs1").status_code == 401


def test_waits_for_export_when_no_zip(admin, bridge):
    assert logic_of(admin) == {"state": "none"}
    assert admin.post("/api/tia/logic/wbs1/rebuild").status_code == 409
    assert admin.get("/api/tia/logic/wbs1").status_code == 404


def test_built_automatically_after_export_and_downloadable(admin, bridge):
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    lg = logic_of(admin)                    # опрос окна видит новый ZIP и собирает файл сам
    assert lg["state"] == "building" or lg["state"] == "ready"
    lg = logic_of(admin)
    assert lg["state"] == "ready" and lg["zip"] == ZIP and lg["blocks"] == 2 and lg["writes"] == 8 and lg["size"] > 100
    r = admin.get("/api/tia/logic/wbs1")
    assert r.status_code == 200 and "logic_wbs1.json" in r.headers["content-disposition"]
    data = r.json()
    assert set(data["blocks"]) == {"Mini_FC", "Sub_FB"}
    assert not list((bridge / "logic").glob("*.tmp"))
    # второй опрос без новой выгрузки заново не собирает
    before = (bridge / "logic" / "wbs1.status.json").read_text(encoding="utf-8")
    logic_of(admin)
    assert (bridge / "logic" / "wbs1.status.json").read_text(encoding="utf-8") == before


def test_rebuild_from_last_zip_replaces_file(admin, bridge):
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    logic_of(admin)
    path = bridge / "logic" / "wbs1.json"
    path.write_text("старое", encoding="utf-8")
    r = admin.post("/api/tia/logic/wbs1/rebuild")
    assert r.status_code == 200 and r.json()["zip"] == ZIP
    assert json.loads(path.read_text(encoding="utf-8"))["blocks"]
    assert logic_of(admin)["state"] == "ready"


def test_broken_zip_gives_error_and_keeps_old_file(admin, bridge):
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    logic_of(admin)
    good = (bridge / "logic" / "wbs1.json").read_bytes()
    make_zip(bridge / "out" / NEW_ZIP, bad=True)
    done_job(bridge, NEW_ZIP, jid="wbs1-20251019-170000", at="2026-10-19T12:00:00+00:00")
    logic_of(admin)
    lg = logic_of(admin)
    assert lg["state"] == "error" and "повреждён" in lg["error"] and lg["zip"] == NEW_ZIP
    assert (bridge / "logic" / "wbs1.json").read_bytes() == good          # прежний файл не тронут
    assert not list((bridge / "logic").glob("*.tmp"))
    assert admin.get("/api/tia/logic/wbs1").status_code == 200            # и всё ещё скачивается
    make_zip(bridge / "out" / NEW_ZIP)                                     # исправили архив, «Пересобрать»
    assert admin.post("/api/tia/logic/wbs1/rebuild").status_code == 200
    assert logic_of(admin)["state"] == "ready"


def test_zip_without_blocks_is_error(admin, bridge):
    with zipfile.ZipFile(bridge / "out" / ZIP, "w") as z:
        z.writestr("manifest.csv", "x")
    done_job(bridge)
    logic_of(admin)
    lg = logic_of(admin)
    assert lg["state"] == "error" and "блок" in lg["error"]


def test_only_listed_project_names_are_used(admin, bridge):
    for pid in ("unknown", "wbs2"):
        assert admin.post(f"/api/tia/logic/{pid}/rebuild").status_code == 404
        assert admin.get(f"/api/tia/logic/{pid}").status_code == 404
    assert admin.get("/api/tia/logic/bad id").status_code == 400
    assert admin.get("/api/tia/logic/..%5Cevil").status_code in (400, 404)
    assert admin.get("/api/tia/logic/..").status_code in (400, 404)
    # проект с недопустимым id в tia-projects.json в списке не появляется и собрать его нельзя
    assert [p["id"] for p in admin.get("/api/tia/projects").json()["projects"]] == ["wbs1"]
    tia.build_logic("../evil", ZIP)                  # даже прямой вызов с чужим именем ничего не пишет за пределами папки logic
    assert not list(bridge.glob("evil*")) and not list(bridge.parent.glob("evil*"))


def test_abandoned_build_is_reported(admin, bridge):
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    (bridge / "logic").mkdir()
    (bridge / "logic" / "wbs1.status.json").write_text(json.dumps(
        {"state": "building", "zip": ZIP, "started_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    lg = logic_of(admin)
    assert lg["state"] == "error" and "прервана" in lg["error"]


def node(expr: str):
    code = f"const t=require({json.dumps(str(STATIC / 'tiaexport.js'))});console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout)


@pytest.mark.skipif(not NODE, reason="нет node")
def test_column_texts():
    p = {"id": "wbs1", "job": {"state": "done"}, "last": {"finished_at": "x", "size": 3.4 * 1048576},
         "logic": {"state": "ready", "finished_at": "y", "size": 2.1 * 1048576, "blocks": 240, "writes": 10582}}
    g = node(f"t.logicInfo({json.dumps(p)}, x => 'T')")
    assert g["cls"] == "tia-ok" and g["text"].startswith("✓ готов T, 2,1 МБ; 240 блоков, ") and "10" in g["text"] and g["text"].endswith("выходов")
    assert node("t.logicInfo({}, null)")["text"] == "— ждёт выгрузки"
    assert node("t.logicInfo({logic:{state:'building'}}, null)")["text"] == "⏳ создаётся…"
    assert node("t.logicInfo({logic:{state:'error',error:'архив повреждён'}}, null)")["text"] == "✗ ошибка: архив повреждён"
    assert node("t.zipInfo({job:{state:'running'}}, null)")["text"] == "⏳ выгружается…"
    assert node(f"t.zipInfo({json.dumps(p)}, x => 'T')")["text"] == "✓ T, 3,4 МБ"
    # кнопки: пересобрать только при готовом ZIP и если сборка не идёт; скачать JSON только готовый файл
    assert node("[t.canRebuild({last:{}}), t.canRebuild({}), t.canRebuild({last:{},logic:{state:'building'}}), t.canDownloadLogic({logic:{state:'ready'}})]") == [True, False, False, True]


def test_window_has_table_with_titles():
    js = (STATIC / "tiaexport.js").read_text(encoding="utf-8")
    for need in ("Файл для ИИ", "Выгрузка (ZIP)", "Скачать JSON", "Пересобрать", "Лежит на сервере: C:\\\\tia-bridge\\\\logic\\\\"):
        assert need in js, need
    assert "/api/tia/logic/" in js


def test_read_only_copy_does_not_build_or_write(admin, bridge, monkeypatch):
    monkeypatch.setattr(tia, "READ_ONLY", True)
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    assert logic_of(admin) == {"state": "readonly"}
    assert not (bridge / "logic").exists()                                 # ни статуса, ни временных файлов
    r = admin.post("/api/tia/logic/wbs1/rebuild")
    assert r.status_code == 409 and "только чтение" in r.json()["detail"]
    assert admin.get("/api/tia/logic/wbs1").status_code == 404


def test_read_only_dir_shows_existing_file_as_ready(admin, bridge, monkeypatch):
    (bridge / "logic").mkdir()
    (bridge / "logic" / "wbs1.json").write_text('{"blocks":{}}', encoding="utf-8")
    make_zip(bridge / "out" / NEW_ZIP)
    done_job(bridge, NEW_ZIP)
    monkeypatch.setattr(tia.os, "access", lambda p, m: False)             # каталог смонтирован :ro
    lg = logic_of(admin)
    assert lg["state"] == "ready" and lg["readonly"] is True and lg["size"] == len('{"blocks":{}}') and lg["finished_at"]
    assert admin.get("/api/tia/logic/wbs1").status_code == 200            # «Скачать JSON» работает
    assert not (bridge / "logic" / "wbs1.status.json").exists()           # автосборка не запускалась
    assert admin.post("/api/tia/logic/wbs1/rebuild").status_code == 409


def test_status_write_failure_gives_error_not_eternal_building(admin, bridge, monkeypatch):
    make_zip(bridge / "out" / ZIP)
    done_job(bridge)
    real = tia._write_json
    calls = []

    def flaky(path, obj):
        calls.append(obj.get("state"))
        if len(calls) == 1:
            raise OSError(30, "Read-only file system")
        real(path, obj)

    monkeypatch.setattr(tia, "_write_json", flaky)
    logic_of(admin)
    lg = logic_of(admin)
    assert lg["state"] == "error" and "Read-only" in lg["error"]
    # запись не удаётся совсем: поток не падает с traceback, а «создаётся» не висит вечно
    monkeypatch.setattr(tia, "_write_json", lambda p, o: (_ for _ in ()).throw(OSError(30, "Read-only file system")))
    tia.build_logic("wbs1", ZIP)
    assert "wbs1" not in tia._logic_busy


@pytest.mark.skipif(not NODE, reason="нет node")
def test_read_only_column():
    ro = {"last": {}, "logic": {"state": "readonly"}}
    assert node(f"t.logicInfo({json.dumps(ro)}, null)")["text"] == "🔒 недоступно в тестовой копии (только чтение)"
    assert node(f"[t.canRebuild({json.dumps(ro)}), t.rebuildTitle({json.dumps(ro)}).split(' ').length >= 10]") == [False, True]
    ready_ro = {"last": {}, "logic": {"state": "ready", "readonly": True, "size": 1, "blocks": 1, "writes": 1}}
    assert node(f"[t.canRebuild({json.dumps(ready_ro)}), t.canDownloadLogic({json.dumps(ready_ro)})]") == [False, True]
