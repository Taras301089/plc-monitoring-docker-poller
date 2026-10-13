"""Тесты скрипта copy-reports.ps1 (копирование отчётов в сетевую папку) и его описания в справке."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "copy-reports.ps1"
REFERENCE = ROOT / "fastapi-api" / "static" / "reference.html"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def _run(*args):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
                          capture_output=True, timeout=120)


def test_syntax_parses():
    cmd = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}', [ref]$null, [ref]$e); "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


def test_static_checks():
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"), "нужен UTF-8 с BOM"
    t = _text()
    assert "&&" not in t and "||" not in t
    assert "/MIR" not in t.upper() and "/PURGE" not in t.upper()
    assert "2>&1" not in t
    assert "/E /XO /R:2 /W:5 /NP /NFL /NDL" in t
    assert "[switch]$DryRun" in t and "[switch]$NoPause" in t
    assert "C:\\Andon-Reports" in t
    assert r"\\alm-mca-fs01.mk.int\Storage\Companies\Astana Motors Manufacturing Kazakhstan\010 Body Shop\020 Production\Автоматические_отчеты" in t
    assert r"C:\plc-backups\update-logs" in t and "copy-reports-" in t
    assert "-ge 8" in t, "код robocopy 8 и больше считается ошибкой"


def test_dry_run_copies_nothing(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "2026-10" / "GWM").mkdir(parents=True)
    (src / "2026-10" / "GWM" / "Отчёт_GWM_2026-10-09.xlsx").write_bytes(b"x")
    dst.mkdir()
    r = _run("-Source", str(src), "-Destination", str(dst), "-DryRun", "-NoPause")
    assert r.returncode == 0, r.stdout.decode("cp866", "replace") + r.stderr.decode("cp866", "replace")
    assert list(dst.rglob("*")) == [], "в назначении ничего не должно появиться"
    assert (src / "2026-10" / "GWM" / "Отчёт_GWM_2026-10-09.xlsx").exists()


def test_dry_run_unavailable_destination_returns_1(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    r = _run("-Source", str(src), "-Destination", str(tmp_path / "нет_такой_папки"), "-DryRun", "-NoPause")
    assert r.returncode == 1


def test_dry_run_unavailable_source_returns_1(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    r = _run("-Source", str(tmp_path / "нет_источника"), "-Destination", str(dst), "-DryRun", "-NoPause")
    assert r.returncode == 1


# ---------- справка ----------

def _section() -> str:
    t = REFERENCE.read_text(encoding="utf-8")
    a = t.index("<h3>Копирование отчётов в сетевую папку</h3>")
    return t[a:t.index("<h3>", a + 10)]


def test_reference_subsection_between_update_and_backup():
    t = REFERENCE.read_text(encoding="utf-8")
    a, b, c = (t.index(f"<h3>{n}</h3>") for n in ("Обновление сервера одной командой", "Копирование отчётов в сетевую папку", "Регулярный бэкап"))
    assert a < b < c


def test_reference_commands_one_per_block_with_copy_buttons():
    s = _section()
    blocks = re.findall(r'<div class="copy-row"><div class="code-block" data-copy>(.*?)</div><button type="button" class="copy-btn" title="([^"]+)">Скопировать</button></div>', s)
    cmds = [b[0] for b in blocks]
    assert len(cmds) == 3
    assert "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\plc-monitoring-docker-poller\\copy-reports.ps1 -DryRun -NoPause" in cmds
    assert any(c.startswith("schtasks /Create /SC DAILY /ST 23:50 /TN \"PMM copy reports\" /TR ") for c in cmds)
    assert 'schtasks /Query /TN "PMM copy reports"' in cmds
    for cmd, title in blocks:
        assert "\n" not in cmd, "одна команда в одном блоке"
        assert len(title.split()) >= 10, "title развёрнутый"
    assert s.count("data-copy") == 3
    assert "23:50" in s and "позже автосохранения" in s


def test_reference_date_and_structure():
    t = REFERENCE.read_text(encoding="utf-8")
    assert "copy-reports.ps1" in t.split("<h3>Копирование отчётов")[0], "строка в структуре каталогов"
    assert "Справка обновлена: <strong>2026-10-12</strong>" in t
    assert "автосохранение одним листом и копирование в сетевую папку" in t
