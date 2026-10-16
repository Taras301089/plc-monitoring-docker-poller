"""Вкладка «Оповещения» (Telegram): чистая логика tgnotify.js через node (разбор «Линий», тихие часы, сборка подписок для PUT)
и статические проверки экрана (подключение в index.html, тайм-ауты запросов, цвета только из переменных). Без БД."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
JS = (STATIC / "tgnotify.js").read_text(encoding="utf-8")
CSS = (STATIC / "tg.css").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")

CATALOG = [
    {"event_type": "alarm_bit", "title": "Авария", "scope_fields": ["brands", "stations", "classes", "plc_ids"], "supports_clear": True},
    {"event_type": "link_lost", "title": "Потеря связи с ПЛК", "scope_fields": ["plc_ids"], "supports_clear": True},
    {"event_type": "voltage_limit", "title": "Напряжение вне порогов", "scope_fields": ["stations", "plc_ids"], "supports_clear": False},
    {"event_type": "system", "title": "Системные события", "scope_fields": [], "supports_clear": False},
]


def run(expr):
    code = f"const c=require({json.dumps(str(STATIC / 'tgnotify.js'))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


@needs_node
def test_scope_key_and_list_parsing():
    assert run("[['brands','stations'],['stations','plc_ids'],['plc_ids'],[],null].map(c.scopeKey)") == ["brands", "stations", "plc_ids", None, None]
    assert run("c.parseList(' GWM, Chery;GWM ,, ')") == ["GWM", "Chery"]
    assert run("c.parseList('')") == [] and run("c.parseList(null)") == []
    assert run("c.listText(['GWM','Chery'])") == "GWM, Chery"
    assert run("c.listError(Array.from({length:51},(_, i)=>'L'+i))") != ""
    assert run("c.listError(['x'.repeat(61)])") != "" and run("c.listError(['GWM'])") == ""


@needs_node
def test_quiet_hours_check_and_range():
    assert run("c.quietCheck('', '')") == {"from": None, "to": None, "error": ""}
    assert run("c.quietCheck('22', '6')") == {"from": 22, "to": 6, "error": ""}
    assert "оба" in run("c.quietCheck('22', '')")["error"]
    assert "0..23" in run("c.quietCheck('24', '6')")["error"]
    assert "0..23" in run("c.quietCheck('1.5', '6')")["error"]
    # через полночь, как in_quiet_hours на сервере; from == to = нет тишины
    assert run("[23,0,5,6,12].map(h=>c.inQuiet(22,6,h))") == [True, True, True, False, False]
    assert run("[8,12,13].map(h=>c.inQuiet(8,13,h))") == [True, True, False]
    assert run("c.inQuiet(5,5,5)") is False and run("c.inQuiet(null,null,5)") is False


@needs_node
def test_build_items_only_enabled_keeps_other_scope_and_also_clear():
    rows = [
        {"event_type": "alarm_bit", "enabled": True, "lines": "GWM, Chery", "quiet_from": "22", "quiet_to": "6"},
        {"event_type": "link_lost", "enabled": True, "lines": "", "quiet_from": "", "quiet_to": ""},
        {"event_type": "voltage_limit", "enabled": False, "lines": "WBS1", "quiet_from": "1", "quiet_to": ""},
        {"event_type": "system", "enabled": True, "lines": "ignored", "quiet_from": "", "quiet_to": ""},
    ]
    prev = [{"event_type": "alarm_bit", "enabled": True, "scope": {"classes": ["A"], "brands": ["Old"], "foreign": ["x"]}, "also_clear": True},
            {"event_type": "link_lost", "enabled": True, "scope": {"plc_ids": ["3"]}, "also_clear": True}]
    r = run(f"c.buildItems({json.dumps(CATALOG)}, {json.dumps(rows)}, {json.dumps(prev)})")
    assert r["error"] == ""
    assert r["items"] == [
        {"event_type": "alarm_bit", "enabled": True, "scope": {"classes": ["A"], "brands": ["GWM", "Chery"]}, "also_clear": True,
         "quiet_from": 22, "quiet_to": 6},
        {"event_type": "link_lost", "enabled": True, "scope": {}, "also_clear": True, "quiet_from": None, "quiet_to": None},
        {"event_type": "system", "enabled": True, "scope": {}, "also_clear": False, "quiet_from": None, "quiet_to": None},
    ]


@needs_node
def test_build_items_reports_quiet_error_with_event_title():
    rows = [{"event_type": "alarm_bit", "enabled": True, "lines": "", "quiet_from": "7", "quiet_to": ""}]
    r = run(f"c.buildItems({json.dumps(CATALOG)}, {json.dumps(rows)}, [])")
    assert r["items"] is None and r["error"].startswith("Авария: ")


@needs_node
def test_rows_from_subscriptions_and_texts():
    subs = [{"event_type": "alarm_bit", "enabled": True, "scope": {"brands": ["GWM"]}, "quiet_from": 0, "quiet_to": 6}]
    rows = run(f"c.rowsFrom({json.dumps(CATALOG)}, {json.dumps(subs)})")
    assert rows[0] == {"event_type": "alarm_bit", "enabled": True, "lines": "GWM", "quiet_from": "0", "quiet_to": "6"}
    assert rows[1] == {"event_type": "link_lost", "enabled": False, "lines": "", "quiet_from": "", "quiet_to": ""}
    assert run("c.testText({queued:true, env:'prod'})") == "поставлено в очередь"
    assert "в разработке не отправляются" in run("c.testText({queued:true, env:'dev'})")
    assert run("c.detailText(0, null)") == "Нет связи с сервером"
    assert run("c.detailText(422, {detail:'Тихие часы: целые числа 0..23'})") == "Тихие часы: целые числа 0..23"
    assert run("c.detailText(422, {detail:[{msg:'bad'}]})") == "bad"


def test_included_in_index_and_placeholder_gone():
    assert '<script src="/static/tgnotify.js"></script>' in INDEX
    assert '<link rel="stylesheet" href="/static/tg.css">' in INDEX
    assert "будущих версиях" not in INDEX


def test_every_fetch_has_timeout_and_refresh():
    assert JS.count("fetch(") == 1 and "AbortSignal.timeout(TIMEOUT)" in JS and "const TIMEOUT = 6000" in JS
    assert "15000" in JS and "5000" in JS
    assert "Войдите, чтобы настроить оповещения" in JS
    assert "tabBtn.addEventListener('click', load)" in JS


def test_css_uses_only_variables():
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", CSS)
    assert "var(--ok)" in CSS and "var(--danger)" in CSS and "var(--accent)" in CSS and "--warn" not in CSS
    assert not re.search(r"font-size:\s*\d", CSS)
