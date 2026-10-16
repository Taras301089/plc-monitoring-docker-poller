"""План на день по интервалам (Andon, окно «Смена и план»): расчёт distributePlan и правка плана интервала по клику в таблице."""
import json
import re
import subprocess
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
MINS = [60, 60, 50, 40, 70, 60, 60, 50, 30]          # интервалы смены GWM (по бумаге мастера)


def _run_many(cases):
    """Все случаи одним запуском Node (раньше процесс запускался на каждый случай: 200 запусков, около 10 с)."""
    m = re.search(r"    function distributePlan\(total, mins\) \{.*?\n    \}\n", HTML, re.S)
    assert m, "функция distributePlan не найдена"
    script = m.group(0) + f"\nconst cases = {json.dumps(cases)};\nconsole.log(JSON.stringify(cases.map(c => distributePlan(c[0], c[1]))));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _run(total, mins):
    return _run_many([[total, mins]])[0]


def test_plan_88_is_close_to_masters_sheet():
    """Мастер на бумаге: 10, 11, 9, 8, 12, 11, 12, 10, 5. Система даёт первый час на 1 меньше и остатки по наибольшей дробной части."""
    assert _run(88, MINS) == [10, 11, 9, 8, 13, 11, 11, 9, 6]


def test_no_extra_one_for_fifth_interval_any_more():
    out = _run(88, MINS)
    assert out[4] <= 13            # раньше пятый интервал получал +1 и выходило 14


def test_sum_is_always_the_total():
    totals = list(range(1, 201))
    for total, out in zip(totals, _run_many([[t, MINS] for t in totals])):
        assert sum(out) == total, total
    assert sum(_run(77, [60, 60, 50])) == 77
    assert sum(_run(5, [30, 30])) == 5


def test_first_interval_is_not_more_than_proportional():
    for total in (40, 55, 66, 77, 88, 99):
        out = _run(total, MINS)
        assert out[0] <= round(total * 60 / sum(MINS)), total


def test_zero_and_negative_totals():
    assert _run(0, MINS) == [0] * 9
    assert _run(-5, MINS) == [0] * 9


def test_inline_edit_of_interval_plan_in_table():
    assert "function editIntervalPlan" in HTML
    body = HTML[HTML.index("function editIntervalPlan"):][:6000]
    assert "canEditPlan()" in body                                # править может тот, кому разрешена правка плана
    assert "/schedule`" in body and "'PUT'" in body               # сохраняется тем же запросом, что и окно «Смена и план»
    assert "Escape" in body and "Enter" in body                   # Enter сохраняет, Esc отменяет
    assert "AbortSignal.timeout(6000)" in body


def test_table_refresh_does_not_kill_the_open_editor():
    i = HTML.index("set(c('plan'), String(cfg.plan))")
    assert "querySelector('input')" in HTML[i - 200: i + 50]      # пока ячейка плана редактируется, перерисовка её не затирает


def test_plan_cell_hint_mentions_click():
    assert "Нажмите, чтобы изменить план этого интервала" in HTML


def test_inline_plan_editor_touch_friendly():
    body = HTML[HTML.index("function editIntervalPlan"):][:6000]
    assert "'inputmode', 'numeric'" in body and "'enterkeyhint', 'done'" in body and "'autocomplete', 'off'" in body
    assert "scrollIntoView" in body and "setTimeout(reveal, 300)" in body
    assert "Сохранить план этого интервала" in body and "Отменить правку плана" in body
    assert "'✓'" in body and "'✕'" in body
    assert "addEventListener('mousedown', e => e.preventDefault())" in body
    assert "addEventListener('pointerdown', e => e.preventDefault())" in body
    assert "wrap.contains(e.relatedTarget)" in body               # blur на кнопку не сохраняет раньше времени


def test_inline_plan_editor_css():
    css = HTML[HTML.index(".plan-inline {"):][:1800]
    assert "appearance: textfield" in css
    assert "::-webkit-inner-spin-button" in css and "::-webkit-outer-spin-button" in css
    assert "font: inherit" in css and "var(--fs-large)" in css and "tabular-nums" in css
    m = re.search(r"\.plan-btn \{ min-width: (\d+)px; min-height: (\d+)px", HTML)
    assert m and int(m.group(1)) >= 36 and int(m.group(2)) >= 36
    assert re.search(r"body\.tv-mode \.plan-btn \{ min-width: 4[4-9]px", HTML)
