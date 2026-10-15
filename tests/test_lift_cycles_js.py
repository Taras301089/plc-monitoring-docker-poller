"""«Тренды» → «Циклы лифтов»: чистая логика trends.js (через node: короткое имя сигнала, ближайший цикл «Норма»,
сдвиг времени завода, секунды) и статические проверки экрана (заголовок, ширины столбцов, тайм-ауты, цвета)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
JS = (STATIC / "trends.js").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")


def run(expr):
    code = f"const c=require({json.dumps(str(STATIC / 'trends.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


def lift_section() -> str:
    return JS.split("// ================= «Циклы лифтов» =================")[1].split("tabBtn.addEventListener")[0]


@needs_node
def test_short_signal_name_is_part_after_db():
    names = ["OPMode08_DB.FK300.AtTop", "ITL_GWM_DB.From GWM.BodyCheck1", "ITL_GWM_DB.RevNewData", "NoDb", None]
    assert run(f"{json.dumps(names)}.map(c.liftShort)") == ["FK300.AtTop", "From GWM.BodyCheck1", "RevNewData", "NoDb", ""]


@needs_node
def test_nearest_ok_cycle_by_start_time_skips_itself_and_not_ok():
    cycles = [
        {"start": "2026-10-15T05:00:00+00:00", "status": "ok"},
        {"start": "2026-10-15T05:10:00+00:00", "status": "problem"},
        {"start": "2026-10-15T05:12:00+00:00", "status": "aborted"},
        {"start": "2026-10-15T05:15:00+00:00", "status": "ok"},
    ]
    near = run(f"c.nearestOk({json.dumps(cycles)}, '2026-10-15T05:12:00+00:00')")
    assert near["start"] == "2026-10-15T05:15:00+00:00"
    # выбранный цикл «Норма» сам себе не пара
    assert run(f"c.nearestOk({json.dumps(cycles)}, '2026-10-15T05:15:00+00:00')")["start"] == "2026-10-15T05:00:00+00:00"
    # нормальных циклов нет
    assert run(f"c.nearestOk({json.dumps(cycles[1:3])}, '2026-10-15T05:10:00+00:00')") is None


@needs_node
def test_plant_offset_from_iso_and_seconds_text():
    assert run("['2026-10-15T00:00:00+05:00','2026-10-15T00:00:00-03:30','2026-10-15T00:00:00Z','2026-10-15T00:00:00'].map(c.isoOffsetMs)") == [
        18000000, -12600000, 0, None]
    assert run("[4.25, 12, 0, null, undefined].map(c.fmtSec)") == ["4,3", "12", "0", "—", "—"]
    assert run("c.LIFT_STATUS") == {"ok": "Норма", "problem": "Проблема", "aborted": "Прерван", "open": "Не закончен"}


def test_section_title_columns_and_widths():
    s = lift_section()
    assert ">Циклы лифтов</h3>" in s
    assert [m for m in re.findall(r"\{ k: '\w+', t: '([^']+)'", s)] == ["Начало", "Ожидание, с", "Итог", "Последний сигнал", "Время сигнала"]
    assert "attachColWidths(host.querySelector('table'), 'lift-cycles')" in s
    assert "sorts: [{ k: 'start', dir: 'desc' }]" in s          # по умолчанию новые сверху
    assert "${lcHtml()}" in JS and "lcInit();" in JS and "lcShow();" in JS


def test_every_fetch_has_timeout_and_refresh_every_30s():
    s = lift_section()
    fetches = re.findall(r"await fetch\(([^;]+)\);", s)
    assert fetches and all("AbortSignal.timeout(6000)" in f for f in fetches)
    assert "30000" in s


def test_lift_css_uses_only_variables():
    css = CSS.split("«Циклы лифтов»")[1].split("\n/*")[0]
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)
    assert "var(--ok)" in css and "var(--danger)" in css and "var(--accent)" in css and "--warn" not in css
