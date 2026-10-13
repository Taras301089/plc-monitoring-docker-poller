"""Дни без выпуска на экране: отметка «Простой весь день (поломка линии)», подпись «нет выпуска» (dayflag.js в node и подключение на экранах)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import dayflags

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = STATIC / "dayflag.js"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
REPORTS = (STATIC / "reports.js").read_text(encoding="utf-8")
ARCHIVE = (STATIC / "archive.js").read_text(encoding="utf-8")
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")

FLAG = {"set_by": 7, "set_by_name": "Иванов, Мастер", "set_at_text": "11.10 14:05"}


def run(expr: str):
    code = f"const r = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True, encoding="utf-8").stdout
    return json.loads(out)


def view(state, flag, user):
    return run(f"r.flagView({json.dumps({'state': state, 'flag': flag})}, {json.dumps(user)})")


def test_screen_labels_equal_server_labels():
    """Подписи экрана и Excel (сервер) одни и те же."""
    src = JS.read_text(encoding="utf-8")
    assert f"const LABEL = '{dayflags.FULL_DOWNTIME_LABEL}';" in src
    assert f"const NO_OUTPUT = '{dayflags.NO_OUTPUT_LABEL}';" in src
    assert f"const FLAG_TITLE = '{dayflags.FLAG_HINT}';" in src
    assert f"const NO_OUTPUT_TITLE = '{dayflags.NO_OUTPUT_HINT}';" in src


@node
def test_button_hidden_when_unavailable():
    master = {"id": 7, "role": "master"}
    assert view(None, None, master) == {"show": False}                         # день с выпуском или текущий
    assert view("no_output", None, None) == {"show": False}                    # нет входа
    assert view("no_output", None, {"id": 1, "role": "viewer"}) == {"show": False}
    assert view("full_downtime", FLAG, None)["show"] is True                   # отмеченное видят все, но нажать не могут
    assert view("full_downtime", FLAG, None)["enabled"] is False


@node
def test_button_states_and_author_note():
    master = {"id": 7, "role": "master"}
    v = view("no_output", None, master)
    assert v["show"] and not v["on"] and v["enabled"] and v["note"] == "" and v["label"] == dayflags.FULL_DOWNTIME_LABEL
    on = view("full_downtime", FLAG, master)
    assert on["on"] and on["enabled"]
    assert on["note"] == "отмечено: Иванов, Мастер · 11.10 14:05"            # содержание, затем автор
    assert view("full_downtime", FLAG, {"id": 8, "role": "chief"})["enabled"] is False     # снять может автор и администратор
    assert view("full_downtime", FLAG, {"id": 8, "role": "admin"})["enabled"] is True


@node
def test_tile_value_and_period_row():
    assert run("r.downValue({no_output: true}, 0, n => String(n))") == "нет выпуска"
    assert run("r.downValue({no_output: true}, 30, n => String(n))") == "30"          # описанное людьми считается
    assert run("r.downValue({no_output: false}, 0, n => String(n))") == "0"
    fmt = "{fmtD: s => s, fmtNum: n => String(n), pct1: v => '0%'}"
    off = run(f"r.periodDayRow({{date: 'd', plan: 13, fact: 0, pct: 0, downtime: 0, described: 0, no_output: true}}, {fmt})")
    assert off == ["d", 13, 0, "", "0%", "нет выпуска", "—", "—"]                     # «±» пустая, простой подписан
    work = run(f"r.periodDayRow({{date: 'd', plan: 13, fact: 12, pct: 0, downtime: 15, described: 5, no_output: false}}, {fmt})")
    assert work == ["d", 13, 12, -1, "0%", "15", "5", "10"]


def test_script_connected_before_reports():
    assert '<script src="/static/dayflag.js"></script>' in HTML
    assert HTML.index("/static/dayflag.js") < HTML.index("/static/reports.js")


def test_reports_screen_uses_flag_and_refreshes():
    assert 'data-f="flag"' in REPORTS and "dayFlag.flagView" in REPORTS and "dayFlag.periodDayRow" in REPORTS
    assert "/day-flag/" in REPORTS and "'DELETE'" in REPORTS and "'PUT'" in REPORTS
    assert "setInterval(" in REPORTS                                                  # перечитывание по таймеру
    m = re.search(r"const REFRESH_MS = (\d+);", REPORTS)
    assert m and int(m.group(1)) <= 10000                                             # правка видна на всех экранах не позднее 10 секунд


def test_archive_shows_no_output_and_rereads():
    assert "data.no_output" in ARCHIVE and "no_output_label" in ARCHIVE
    m = re.search(r"const REFRESH_MS = (\d+);", ARCHIVE)
    assert m and int(m.group(1)) <= 10000
    assert "AbortSignal.timeout(6000)" in ARCHIVE                                      # тайм-аут запроса не больше 6 секунд
    dt = (STATIC / "downtime.js").read_text(encoding="utf-8")
    assert int(re.search(r"const REFRESH_MS = (\d+);", dt).group(1)) <= 10000
