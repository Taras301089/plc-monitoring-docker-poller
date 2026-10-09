"""Тесты скрипта update-server.ps1 (обновление сервера одной командой)."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "update-server.ps1"
REFERENCE = ROOT / "fastapi-api" / "static" / "reference.html"
BACKUPS = Path(r"C:\plc-backups")


def _run(args, timeout=120):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=ROOT, capture_output=True, timeout=timeout,
    )


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_syntax_parses():
    cmd = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}', [ref]$null, [ref]$e); "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


def test_static_checks():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "нужен UTF-8 с BOM"
    t = _text()
    backup_call = t.index("-File $backupScript")
    assert backup_call < t.index("& git pull --ff-only"), "бэкап должен идти раньше git pull"
    assert backup_call < t.index("& git fetch")
    assert "--ff-only" in t
    assert "reset --keep" in t and "reset --hard" not in t
    assert "APP_ENV=prod" in t
    assert "/api/env" in t and "/api/plcs" in t
    assert "-DryRun" in t and "-NoPause" in t
    assert "[switch]$DryRun" in t and "[switch]$NoPause" in t
    assert "n8n_data" in t and "grafana_data" in t
    assert "&&" not in t and "||" not in t
    assert "-TimeoutSec 6" in t and "-UseBasicParsing" in t
    assert "2>&1" not in t


def _snapshot():
    st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, check=True).stdout
    env = ROOT / ".env"
    env_state = env.read_bytes() if env.exists() else None
    dumps = sorted(
        (p.name, p.stat().st_mtime) for p in BACKUPS.glob("*")
        if p.is_file()
    ) if BACKUPS.exists() else []
    return st, env_state, dumps


def test_dry_run_changes_nothing():
    before = _snapshot()
    r = _run(["-File", str(SCRIPT), "-DryRun", "-NoPause"])
    out = r.stdout.decode("cp866", "replace")  # консоль Windows отдаёт OEM-кодировку
    assert r.returncode == 0, out + r.stderr.decode("cp866", "replace")
    assert "DryRun" in out
    for n in range(1, 11):
        assert f"Шаг {n} из 10" in out, f"нет шага {n}"
    after = _snapshot()
    assert after[0] == before[0], "git status изменился"
    assert after[1] == before[1], ".env изменился"
    assert after[2] == before[2], "в C:\\plc-backups изменились дампы или status.json"


def test_reference_mentions_script():
    t = REFERENCE.read_text(encoding="utf-8")
    assert "update-server.ps1" in t
    assert "Обновление сервера одной командой" in t


def test_force_switch_rebuilds_even_when_up_to_date():
    """Первый запуск после ручного git pull: без -Force скрипт увидел бы «версия уже последняя» и не пересобрал бы образы."""
    script = (Path(__file__).resolve().parent.parent / "update-server.ps1").read_text(encoding="utf-8-sig")
    assert "[switch]$Force" in script
    assert "-and $Force" in script and "указан -Force" in script


def test_unexpected_error_after_pull_triggers_rollback():
    script = (Path(__file__).resolve().parent.parent / "update-server.ps1").read_text(encoding="utf-8-sig")
    assert "$script:pulledFrom = $prev" in script
    assert "Invoke-Rollback $script:pulledFrom" in script
    assert "$script:rolledBack = $true" in script


def test_reference_explains_first_run_with_force():
    ref = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "reference.html").read_text(encoding="utf-8")
    assert "update-server.ps1" in ref and "-Force" in ref and "Первый раз" in ref
