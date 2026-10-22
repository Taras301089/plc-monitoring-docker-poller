"""Тесты backup.ps1: запуск при входе (-OnlyIfMissing), набор конфигурации config_*.zip, status.json.

Все запуски идут во временную папку (-Dir), рабочая C:\\plc-backups не затрагивается.
"""
import json
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "backup.ps1"
SECRET = "SECRET-VALUE-do-not-leak-4711"


def _run(*args, timeout=180):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
        capture_output=True, timeout=timeout, cwd=ROOT,
    )


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "inspect", "postgres_db", "--format", "{{.State.Running}}"], capture_output=True)
    return r.returncode == 0 and b"true" in r.stdout


def _has_alpine_image() -> bool:
    # backup.ps1 пакует данные n8n и Grafana через локальный образ на alpine; на ПК разработки его может не быть
    r = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True)
    have = set(r.stdout.decode("utf-8", "replace").split())
    return bool(have & {"alpine:latest", "python:3.12-alpine", "node:20-alpine"})


def test_syntax_and_bom():
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"), "нужен UTF-8 с BOM"
    cmd = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}', [ref]$null, [ref]$e); "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


def test_static_config_only_to_network_folder():
    t = _text()
    for p in ("[switch]$OnlyIfMissing", "[switch]$ConfigOnly", "[string]$Dir = 'C:\\plc-backups'"):
        assert p in t, p
    net = next(l for l in t.splitlines() if "name = 'Сетевая папка'" in l)
    gd = next(l for l in t.splitlines() if "name = 'Google Drive'" in l)
    assert "includeConfig = $true" in net
    assert "includeConfig = $false" in gd
    assert "$t.includeConfig -and $configName" in t, "набор копируется только туда, где разрешён"


def test_only_if_missing_exits_without_backup(tmp_path):
    day = date.today().isoformat()
    for n in ("hub", "n8n"):
        (tmp_path / f"{n}_{day}.dump").write_bytes(b"x" * 2048)
    r = _run("-OnlyIfMissing", "-Dir", str(tmp_path))
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    assert not (tmp_path / "status.json").exists(), "status.json не должен перезаписываться"
    assert not list(tmp_path.glob("config_*.zip"))
    log = (tmp_path / "backup.log").read_text(encoding="utf-8-sig")
    assert "OnlyIfMissing" in log and "бэкап не нужен" in log
    assert (tmp_path / f"hub_{day}.dump").stat().st_size == 2048, "дамп не перезаписан"


@pytest.mark.skipif(not _docker_ready(), reason="нужен запущенный контейнер postgres_db (только чтение)")
def test_config_set_manifest_status_and_targets(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text(f"APP_ENV=prod\nTELEGRAM_TOKEN={SECRET}\n", encoding="utf-8")
    net = tmp_path / "net"
    gd = tmp_path / "gd"
    net.mkdir()
    gd.mkdir()
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([
        {"name": "net", "keepDays": 7, "includeConfig": True, "path": str(net)},
        {"name": "gd", "keepDays": 7, "includeConfig": False, "path": str(gd)},
    ]), encoding="utf-8")
    bk = tmp_path / "bk"
    r = _run("-ConfigOnly", "-Dir", str(bk), "-ProjectDir", str(proj), "-TargetsFile", str(targets))
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, out
    assert SECRET not in out, "секрет попал в вывод"

    day = date.today().isoformat()
    zname = f"config_{day}.zip"
    with zipfile.ZipFile(bk / zname) as z:
        names = set(z.namelist())
        manifest = z.read("manifest.txt").decode("utf-8-sig")
        assert SECRET.encode() in z.read(".env"), ".env должен лежать в наборе"
    assert {".env", "globals.sql", "manifest.txt"} <= names
    if _has_alpine_image():
        assert "grafana_data.tgz" in names and "n8n_data.tgz" in names
    else:
        assert "ПРОПУЩЕНО  grafana_data.tgz" in manifest and "нет локального образа alpine" in manifest
        assert "ПРОПУЩЕНО  n8n_data.tgz" in manifest
    assert any(n.startswith("scheduled-tasks/") for n in names) or any(n.startswith("scheduled-tasks\\") for n in names)
    assert "вошло      .env" in manifest and "globals.sql" in manifest
    assert "Не вошло:" in manifest and "tia-bridge" in manifest
    assert SECRET not in manifest, "секрет попал в manifest"
    log = (bk / "backup.log").read_text(encoding="utf-8-sig")
    assert SECRET not in log

    # набор идёт только в сетевую папку, на «Google Drive» его нет
    assert (net / zname).exists()
    assert not list(gd.glob("config_*"))

    # status.json: прежние поля на месте, добавлено config
    st = json.loads((bk / "status.json").read_text(encoding="utf-8-sig"))
    for k in ("time", "ok", "error", "folder", "keep_days", "disk_total", "disk_free", "files", "copies"):
        assert k in st, k
    assert st["config"]["file"] == zname and st["config"]["size"] > 0
    assert isinstance(st["config"]["missing"], list)
    assert not (Path("C:/pmm-cfg-stage")).exists(), "временная папка сборки должна удаляться"


def test_config_failure_does_not_fail_backup_part(tmp_path):
    # без .env и с несуществующим docker-образом/контейнерами набор не собирается целиком, но скрипт не падает
    bk = tmp_path / "bk"
    targets = tmp_path / "targets.json"
    targets.write_text("[]", encoding="utf-8")
    r = _run("-ConfigOnly", "-Dir", str(bk), "-ProjectDir", str(tmp_path / "nope"), "-TargetsFile", str(targets))
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")
    st = json.loads((bk / "status.json").read_text(encoding="utf-8-sig"))
    assert ".env" in st["config"]["missing"]
