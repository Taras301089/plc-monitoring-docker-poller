"""Выгрузка проекта TIA (кнопка и окно во вкладке «ПЛК»): проверка пути сохранения (tia.check_target и Test-TargetDir в tia-bridge.ps1), поле plc проекта,
копия ZIP в указанную папку, чистая логика tiaexport.js (через node), кнопка и окно. База не нужна."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import tia

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
PS = (ROOT / "tia-bridge.ps1").read_text(encoding="utf-8-sig")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
REF = (STATIC / "reference.html").read_text(encoding="utf-8")
JS = (STATIC / "tiaexport.js").read_text(encoding="utf-8")
ZIP = "TIA_wbs1_2026-10-16_09-30.zip"
NODE = shutil.which("node")
PWSH = shutil.which("powershell") or shutil.which("pwsh")

GOOD = [
    (r"C:\tia-bridge\out", r"C:\tia-bridge\out"),
    ("c:/data/tia/", r"C:\data\tia"),
    (r"D:\TIA\Выгрузки", r"D:\TIA\Выгрузки"),
    (r"\\srv\share", r"\\srv\share"),
    ("\\\\srv\\share\\x\\", r"\\srv\share\x"),
    (r"C:\Users\Op\Documents\TIA", r"C:\Users\Op\Documents\TIA"),
    (r"C:\Program\x", r"C:\Program\x"),
    ("  ", None),
    (None, None),
]
BAD = [
    r"C:", "C:\\", r"relative\x", r"\srv\share", r"\\srv", r"C:\a\..\b", r"\\srv\share\..\x", r"C:\a\.\b", r"C:\a\\b",
    r"C:\Windows\Temp", r"c:\windows", r"D:\Program Files\x", r"C:\Program Files (x86)\a", r"C:\ProgramData\pmm",
    r"C:\Users", r"C:\Users\Op", r"C:\a:b", r"C:\a\b?", 'C:\\a"b', "C:\\" + "a" * 240,
]


@pytest.mark.parametrize("raw,want", GOOD)
def test_check_target_accepts_and_normalizes(raw, want):
    assert tia.check_target(raw) == want


@pytest.mark.parametrize("raw", BAD)
def test_check_target_rejects_with_message(raw):
    with pytest.raises(ValueError) as e:
        tia.check_target(raw)
    assert len(str(e.value)) > 15


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "bridge.json").write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    (tmp_path / "tia-projects.json").write_text(json.dumps({"projects": [
        {"id": "wbs1", "name": "AE · WBS1", "plc": " AE_WBS_1 ", "path": "C:/TIA/WBS1.ap17"},
        {"id": "old", "name": "Старый", "path": "C:/TIA/old.ap17"},
    ]}, ensure_ascii=False), encoding="utf-8")
    app = FastAPI()
    app.include_router(tia.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"last_name": "Исимов", "login": "isimov", "role": "admin"}
    return TestClient(app), tmp_path


def test_plc_field_is_optional_and_trimmed(client):
    c, _ = client
    r = c.get("/api/tia/projects").json()
    assert r["default_target"] == r"C:\tia-bridge\out"
    by = {p["id"]: p for p in r["projects"]}
    assert by["wbs1"]["plc"] == "AE_WBS_1" and by["old"]["plc"] is None       # старый файл без plc работает
    assert by["wbs1"]["target"] is None and "path" not in by["wbs1"]


def test_export_rejects_bad_target_with_422(client):
    c, bridge = client
    r = c.post("/api/tia/export", json={"project_ids": ["wbs1"], "target_dir": r"C:\Windows\x"})
    assert r.status_code == 422 and "Системная папка" in r.json()["detail"]
    assert not (bridge / "requests").exists()                                  # заявка не создана


def test_export_passes_target_and_reports_copy(client):
    c, bridge = client
    r = c.post("/api/tia/export", json={"project_ids": ["wbs1"], "target_dir": "d:/TIA/out/"})
    assert r.status_code == 200
    jid = r.json()["started"][0]
    req = json.loads((bridge / "requests" / f"{jid}.json").read_text(encoding="utf-8"))
    assert req["target"] == r"D:\TIA\out" and "path" not in req
    p = c.get("/api/tia/projects").json()["projects"][0]
    assert p["job"]["target"] == r"D:\TIA\out" and p["target"] == r"D:\TIA\out"   # путь по умолчанию в следующий раз
    st = json.loads((bridge / "jobs" / jid / "status.json").read_text(encoding="utf-8"))
    st.update(state="done", finished_at=datetime.now(timezone.utc).isoformat(), file=ZIP, size=2048, exported=5, skipped=1, failed=0,
              texts=2, prodiag=True, saved_to=r"D:\TIA\out" + "\\" + ZIP)
    (bridge / "jobs" / jid / "status.json").write_text(json.dumps(st), encoding="utf-8")
    (bridge / "out" / ZIP).write_bytes(b"PK" + b"0" * 2046)
    last = c.get("/api/tia/projects").json()["projects"][0]["last"]
    assert last["target"] == r"D:\TIA\out" and last["saved_to"].endswith(ZIP) and last["texts"] == 2 and last["prodiag"] is True
    # без target_dir заявка как раньше
    r2 = c.post("/api/tia/export", json={"project_ids": ["old"]})
    assert r2.status_code == 200
    req2 = json.loads((bridge / "requests" / f"{r2.json()['started'][0]}.json").read_text(encoding="utf-8"))
    assert "target" not in req2


def test_service_checks_target_and_copies_zip():
    assert "function Test-TargetDir" in PS and "function Test-TargetWritable" in PS
    assert "Test-TargetDir ([string]$req.target)" in PS and "Папка недоступна для записи" in PS
    assert "New-Item -ItemType Directory -Force -Path $Dir" in PS and ".pmm-write-test-" in PS
    # проверка до запуска TIA: Test-TargetWritable стоит раньше Start-Process
    assert PS.index("Test-TargetWritable $target") < PS.index("Start-Process -FilePath 'powershell.exe'")
    assert "Copy-Item -LiteralPath $zip -Destination $dest" in PS and "saved_to = $savedTo" in PS and "target = $target" in PS
    assert "$r.kind -eq 'texts'" in PS and "$r.kind -eq 'prodiag'" in PS
    assert "Select-Object -Skip $KeepZips" in PS                               # в out\ по-прежнему хранится KeepZips штук
    for name in tia._SYSTEM_TOP:                                               # те же системные папки, что на сайте
        assert f"'{name}'" in PS, name
    assert "StartsWith('program files')" in PS and "-gt 240" in PS


@pytest.mark.skipif(not PWSH, reason="нет PowerShell")
def test_powershell_rules_match_python(tmp_path):
    cases = [g for g, _ in GOOD if g and g.strip()] + BAD
    script = tmp_path / "t.ps1"
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    script.write_text(
        "$e=$null; $ast=[System.Management.Automation.Language.Parser]::ParseFile('" + str(ROOT / "tia-bridge.ps1") + "',[ref]$null,[ref]$e)\n"
        "$utf8 = New-Object System.Text.UTF8Encoding($false)\n"
        "foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $args[0].Name -eq 'Test-TargetDir' }, $true)) { Invoke-Expression $f.Extent.Text }\n"
        "$res = foreach ($c in (Get-Content -LiteralPath '" + str(data) + "' -Raw -Encoding UTF8 | ConvertFrom-Json)) { $r = Test-TargetDir $c; [string]$r.path }\n"
        "[IO.File]::WriteAllText('" + str(tmp_path / "out.json") + "', (ConvertTo-Json @($res)), $utf8)\n",
        encoding="utf-8-sig")
    subprocess.run([PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], check=True, timeout=60, capture_output=True)
    got = json.loads((tmp_path / "out.json").read_text(encoding="utf-8-sig"))
    want = [tia.check_target(c) if c not in BAD else None for c in cases]
    assert got == [w or "" for w in want]


def node(expr: str):
    code = (f"const t=require({json.dumps(str(STATIC / 'tiaexport.js'))});"
            f"console.log(JSON.stringify({expr}))")
    return json.loads(subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout)


@pytest.mark.skipif(not NODE, reason="нет node")
def test_default_target_and_can_export():
    assert node("[t.defaultTarget({target:'D:\\\\A'}), t.defaultTarget({last:{target:'E:\\\\B'}}), t.defaultTarget({}, 'C:\\\\x'), t.defaultTarget(null)]") == [
        r"D:\A", r"E:\B", r"C:\x", r"C:\tia-bridge\out"]


@pytest.mark.skipif(not NODE, reason="нет node")
def test_state_and_last_texts():
    assert node("['queued','running','done'].map(s => t.stateInfo({state:s}).text).concat([t.stateInfo(null).text, t.stateInfo({state:'error', error:'нет HSP'}).text])") == [
        "в очереди", "выгружается…", "готово", "не выгружался", "ошибка: нет HSP"]
    last = {"finished_at": "X", "size": 1572864, "exported": 10, "skipped": 2, "failed": 1, "texts": 2, "prodiag": True, "file": ZIP}
    assert node(f"t.lastText({json.dumps(last)}, x => '16.10.2026 09:30')") == (
        "Последняя выгрузка: 16.10.2026 09:30 · 1,5 МБ · блоков 10, пропущено 2, не выгружено 1 · файлов Excel: тексты проекта, ProDiag")
    assert node("[t.excelText({texts:0, prodiag:false}), t.excelText({texts:1}), t.excelText({prodiag:true}), t.excelText(null)]") == [
        "нет", "тексты проекта", "ProDiag", "нет"]
    assert node(f"[t.savedPath({json.dumps(last)}), t.savedPath({{saved_to:'D:\\\\T\\\\a.zip'}}), t.savedPath(null)]") == [
        "C:\\tia-bridge\\out\\" + ZIP, r"D:\T\a.zip", ""]


def test_button_in_plc_row_and_modal_no_separate_tab():
    btn = re.search(r'<button type="button" id="tia-open" hidden disabled title="([^"]+)">🧩 Выгрузить проект TIA</button>', HTML)
    assert btn and 10 <= len(btn.group(1).split()) <= 25
    row = HTML.split('class="area-plc-body"')[1].split("</section>")[0]                # в строке выбора ПЛК, после «Обновить с ПЛК»
    assert row.index('id="refresh"') < row.index('id="tia-open"')
    # отдельной вкладки нет
    assert 'data-tab="tia"' not in HTML and 'id="tia"' not in HTML and "withNew" not in HTML
    assert "withNew" not in (STATIC / "reorder.js").read_text(encoding="utf-8")
    assert "'tia'" not in (STATIC / "authtabs.js").read_text(encoding="utf-8")
    assert "tab-tia" not in (STATIC / "refnav.js").read_text(encoding="utf-8") and "tab-tia" not in REF
    # окно в стиле диалогов сайта: закрытие Esc, щелчком вне окна и кнопкой «Закрыть»; только администратор
    assert "kpi-modal-box auth-modal tia-box" in JS and ">Закрыть</button>" in JS and "e.key === 'Escape'" in JS and "e.target === modal" in JS
    assert "isAdminUser" in JS and "querySelector('#tia-open')" in JS and "#tia'" not in JS
    assert "Ни для одного ПЛК не задан проект" not in JS and "hasData()" in JS        # кнопка доступна и без проектов: проект выбирают из найденных
    assert "AbortSignal.timeout(6000)" in JS and "FAST_MS = 4000" in JS
    for el in ("tia-src", "tia-target", "tia-go", "tia-dl"):
        assert re.search(rf'id="{el}"[^>]*title="[^"]{{40,}}"', JS), el


def test_plc_tab_renamed_everywhere():
    assert re.search(r'data-tab="browser" id="tab-browser" hidden title="[^"]{40,}">📊 ПЛК</button>', HTML)
    assert re.search(r'id="tab-browser" hidden href="index.html#browser" title="[^"]{40,}">📊 ПЛК</a>', REF)
    for f in STATIC.glob("*.js"):
        assert "Переменные ПЛК" not in f.read_text(encoding="utf-8"), f.name
    assert "Переменные ПЛК" not in HTML
    # в справке старое имя осталось только в истории журнала изменений
    body = REF.split('<ul id="changelog">')[0]
    assert "Переменные ПЛК" not in body
    assert "Переменные БД" in HTML                                             # вкладку «Переменные БД» не трогали


def test_reference_describes_tab():
    sec = REF.split("<h3>Выгрузка проектов TIA Portal в XML</h3>")[1].split("<h3>")[0]
    for w in ("«🧩 Выгрузить проект TIA»", "«ПЛК»", "Проект (папка)", "found-projects.json", "roots", "Закрыть", "Путь сохранения", "Выгрузить", "ProjectTexts_", "ProDiag_Supervisions.xlsx", "manifest.csv", "таблиц тегов"):
        assert w in sec, w
    assert re.search(r'<li data-date="2026-10-16">выгрузка проекта TIA: во вкладке «ПЛК»[^<]*кнопка «🧩 Выгрузить проект TIA»', REF)
    assert "Выгрузка TIA»" not in REF                                         # отдельной вкладки больше нет
    assert "tiaexport.js, tia.css" in REF and "tia.py" in REF


def test_zip_button_mentions_excel_and_css_tokens():
    m = re.search(r'id="tia-dl" title="([^"]+)"', JS)
    assert m and "ProjectTexts_" in m.group(1) and "ProDiag_Supervisions.xlsx" in m.group(1)
    css = (STATIC / "tia.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", css) and not re.search(r"font-size:\s*\d+px", css)


def test_window_explains_and_shows_json_path():
    # описание окна, точный путь файла для ИИ в строке готового проекта и подсказка, как добавить проект
    for t in ("Что здесь происходит", r"C:\\tia-bridge\\out", r"C:\\tia-bridge\\logic\\&lt;проект&gt;.json", "Скачать JSON",
              "по нему локальная модель объяснила, почему встала станция", r"C:\\tia-bridge\\tia-projects.json", "tia-fpath", "(на сервере)"):
        assert t in JS, t
    css = (STATIC / "tia.css").read_text(encoding="utf-8")
    assert ".tia-intro" in css and ".tia-fpath" in css and "var(--fs-small)" in css.split(".tia-intro")[1].split("}")[0]


@pytest.mark.skipif(NODE is None, reason="нужен node")
def test_logic_path_and_hint():
    code = f"const r = require({json.dumps(str(STATIC / 'tiaexport.js'))}); console.log(JSON.stringify([r.logicPath({{id: 'wbs1'}}), r.logicPath(null), r.foundHint(null)]))"
    out = json.loads(subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30, check=True, encoding="utf-8").stdout)
    assert out[0] == r"C:\tia-bridge\logic\wbs1.json" and out[1] == ""
    assert r"C:\tia-bridge\tia-projects.json" in out[2] and "администратор" in out[2]
