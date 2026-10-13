"""Справка: у команд обновления сервера есть кнопка «Скопировать» (статические проверки)."""
import pathlib
import re

REF = pathlib.Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "reference.html"


def _section():
    s = REF.read_text(encoding="utf-8")
    a = s.index("<h3>Обновление сервера одной командой</h3>")
    b = s.index("<h3>Регулярный бэкап</h3>")
    return s[a:b], s


def test_every_block_has_copy_button_with_title():
    sec, _ = _section()
    blocks = re.findall(r'<div class="code-block" data-copy>', sec)
    buttons = re.findall(r'<button[^>]*class="copy-btn"[^>]*>Скопировать</button>', sec)
    assert len(blocks) >= 6
    assert len(buttons) == len(blocks)
    for b in buttons:
        title = re.search(r'title="([^"]+)"', b).group(1)
        assert len(title.split()) >= 10


def test_no_plain_code_blocks_in_section():
    sec, _ = _section()
    assert '<div class="code-block">' not in sec


def test_one_command_per_block():
    sec, _ = _section()
    for cmd in re.findall(r'<div class="code-block" data-copy>(.*?)</div>', sec, flags=re.S):
        assert "\n" not in cmd.strip(), cmd


def test_first_update_commands_present():
    sec, _ = _section()
    assert ">git pull</div>" in sec
    assert "update-server.ps1 -DryRun</div>" in sec
    assert "update-server.ps1 -Force</div>" in sec


def test_hint_mentions_powershell_and_paste():
    sec, _ = _section()
    assert "PowerShell" in sec and "вставьте" in sec


def test_fallback_for_insecure_http():
    _, s = _section()
    assert "execCommand('copy')" in s
    assert "navigator.clipboard" in s and "isSecureContext" in s
    assert "Скопировано ✓" in s
    assert "Не удалось, выделите и скопируйте вручную" in s


def test_buttons_do_not_overlap_text():
    _, s = _section()
    row = re.search(r"\.copy-row \{([^}]*)\}", s).group(1)
    assert "flex-wrap: wrap" in row
    btn = re.search(r"\.copy-btn \{([^}]*)\}", s).group(1)
    assert "absolute" not in btn and "fixed" not in btn
