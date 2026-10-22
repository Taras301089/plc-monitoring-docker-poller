"""Выбор проекта TIA из найденных: служба tia-bridge.ps1 ищет *.ap17 в разрешённых папках (found-projects.json), сайт принимает только ключ
найденного файла (не путь) и имя ПЛК, служба ещё раз проверяет путь и записывает пару «ПЛК -> проект» в tia-projects.json.
Всё во временных папках: рабочие tia-projects.json и found-projects.json, TIA Portal и сама служба не запускаются. База не нужна."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import tia

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
PS_PATH = ROOT / "tia-bridge.ps1"
PS = PS_PATH.read_text(encoding="utf-8-sig")
JS = (STATIC / "tiaexport.js").read_text(encoding="utf-8")
PWSH = shutil.which("powershell") or shutil.which("pwsh")
NODE = shutil.which("node")
KEY = r"TIA_for_claude_to_XML\GWM\UB\UB.ap17"


def _found(bridge, items=None, ago=0, alive=True):
    when = (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat()
    items = items if items is not None else [
        {"key": KEY, "root": "TIA_for_claude_to_XML", "rel": r"GWM\UB\UB.ap17", "path": r"C:\TIA_for_claude_to_XML\GWM\UB\UB.ap17",
         "modified": "2026-10-20T07:03:00Z", "size": 1000},
        {"key": r"TIA_for_claude_to_XML\ASTANA\W1\W1.ap17", "root": "TIA_for_claude_to_XML", "rel": r"ASTANA\W1\W1.ap17",
         "path": r"C:\TIA_for_claude_to_XML\ASTANA\W1\W1.ap17", "modified": "2026-10-19T07:03:00Z", "size": 2000}]
    (bridge / "found-projects.json").write_text(json.dumps({"time": when, "roots": [r"C:\TIA_for_claude_to_XML", r"C:\projects"], "projects": items}), encoding="utf-8")
    if alive:
        (bridge / "bridge.json").write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tia, "BRIDGE_DIR", tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "tia-projects.json").write_text(json.dumps({"projects": [
        {"id": "wbs1", "name": "AE · WBS1", "plc": "AE_WBS_1", "path": r"C:\TIA_for_claude_to_XML\ASTANA\W1\W1.ap17"},
        {"id": "old", "name": "Старый", "plc": "OLD_PLC", "path": r"D:\elsewhere\old\old.ap17"}]}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"last_name": "Исимов", "login": "isimov", "role": "admin"}
    return TestClient(app)


def _reqs(bridge):
    return [json.loads(f.read_text(encoding="utf-8")) for f in (bridge / "requests").glob("*.json")]


# ---------- сайт: found в API ----------

def test_projects_return_found_without_paths(client, bridge):
    _found(bridge)
    r = client.get("/api/tia/projects").json()
    f = r["found"]
    assert f["stale"] is False and f["roots"] == [r"C:\TIA_for_claude_to_XML", r"C:\projects"]
    assert [i["rel"] for i in f["items"]] == [r"GWM\UB\UB.ap17", r"ASTANA\W1\W1.ap17"]
    assert all("path" not in i and "_path" not in i and i["key"] for i in f["items"])         # полный путь клиенту не отдаётся
    by = {p["id"]: p for p in r["projects"]}
    assert by["wbs1"]["found_key"] == r"TIA_for_claude_to_XML\ASTANA\W1\W1.ap17" and by["wbs1"]["folder"] == "W1"
    assert by["old"]["found_key"] is None and by["old"]["folder"] == "old" and "path" not in by["old"]


def test_found_is_stale_when_old_missing_or_service_dead(client, bridge):
    assert client.get("/api/tia/projects").json()["found"] == {"time": None, "stale": True, "roots": [], "items": []}      # файла нет
    _found(bridge, ago=tia.FOUND_STALE_SEC + 60)
    assert client.get("/api/tia/projects").json()["found"]["stale"] is True                                               # старше 5 минут
    _found(bridge, ago=10)
    assert client.get("/api/tia/projects").json()["found"]["stale"] is False
    (bridge / "bridge.json").write_text(json.dumps({"time": (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()}), encoding="utf-8")
    assert client.get("/api/tia/projects").json()["found"]["stale"] is True                                               # службы нет
    (bridge / "found-projects.json").write_text("{битый json", encoding="utf-8")
    assert client.get("/api/tia/projects").json()["found"]["items"] == []


# ---------- сайт: выгрузка по ключу ----------

def test_export_by_key_sends_plc_and_key_never_a_path(client, bridge):
    _found(bridge)
    r = client.post("/api/tia/export", json={"plc": "GWM_UB", "found_key": KEY, "path": r"C:\Windows\evil.ap17", "target_dir": r"D:\TIA\out"})
    assert r.status_code == 200
    (req,) = _reqs(bridge)
    assert req["project"] == "gwm_ub" and req["plc"] == "GWM_UB" and req["found_key"] == KEY and req["target"] == r"D:\TIA\out"
    assert "path" not in req and "ap17" not in json.dumps({k: v for k, v in req.items() if k != "found_key"})
    st = json.loads((bridge / "jobs" / req["id"] / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "queued" and st["project"] == "gwm_ub"


def test_export_for_plc_with_pair_reuses_its_project_id(client, bridge):
    _found(bridge)
    assert client.post("/api/tia/export", json={"plc": "ae_wbs_1", "found_key": KEY}).status_code == 200
    (req,) = _reqs(bridge)
    assert req["project"] == "wbs1"                       # история выгрузок и файл для ИИ остаются на прежнем id


def test_export_rejects_foreign_key_missing_plc_and_empty_choice(client, bridge):
    _found(bridge)
    r = client.post("/api/tia/export", json={"plc": "GWM_UB", "found_key": r"C:\Windows\evil.ap17"})
    assert r.status_code == 404 and "найденных" in r.json()["detail"]
    assert client.post("/api/tia/export", json={"found_key": KEY}).status_code == 422                # без ПЛК
    assert client.post("/api/tia/export", json={"plc": "  ", "found_key": KEY}).status_code == 422
    assert client.post("/api/tia/export", json={"plc": "Ж_Ж", "found_key": KEY}).status_code == 422  # из имени нельзя собрать id
    assert client.post("/api/tia/export", json={}).status_code == 422
    assert not (bridge / "requests").exists()


def test_export_by_key_needs_admin_and_service(bridge):
    app = FastAPI()
    app.include_router(tia.router)
    anon = TestClient(app)
    _found(bridge)
    assert anon.post("/api/tia/export", json={"plc": "GWM_UB", "found_key": KEY}).status_code == 401
    app.dependency_overrides[auth.require_admin] = lambda: {"last_name": "И", "login": "i", "role": "admin"}
    (bridge / "bridge.json").write_text(json.dumps({"time": (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()}), encoding="utf-8")
    assert anon.post("/api/tia/export", json={"plc": "GWM_UB", "found_key": KEY}).status_code == 503


def test_export_by_known_project_id_still_works(client, bridge):
    _found(bridge)
    assert client.post("/api/tia/export", json={"project_ids": ["old"]}).status_code == 200      # пара вне найденных папок
    assert client.post("/api/tia/export", json={"project_ids": ["nope"]}).status_code == 404


# ---------- служба: текст скрипта ----------

def test_service_text_rules():
    for fn in ("Get-Roots", "Test-InsideRoot", "Test-Reparse", "Find-Ap17", "Invoke-Scan", "Set-ProjectPair", "Resolve-FoundProject"):
        assert f"function {fn}" in PS, fn
    assert "found-projects.json" in PS and "$DefaultRoots = @('C:\\TIA_for_claude_to_XML', 'C:\\projects')" in PS
    assert "$ScanMaxDepth = 5" in PS and "$ScanEverySec" in PS and "-ge $ScanEverySec" in PS
    for skip in ("'im'", "'xref'", "'system'", "'logs'", "'temp'"):
        assert skip in PS, skip
    assert "$cfg.roots" in PS and "history" in PS and "Select-Object -First 5" in PS
    # путь из заявки не берётся: проект ищется в found-projects.json по ключу и проверяется заново (до записи пары и до запуска TIA)
    assert "$req.path" not in PS and "$Req.found_key" in PS
    assert PS.index("Resolve-FoundProject $req") < PS.index("Start-Process -FilePath 'powershell.exe'")
    body = PS[PS.index("function Resolve-FoundProject"):PS.index("function Invoke-Job")]
    assert ".ap17" in body and "Test-Path -LiteralPath $path -PathType Leaf" in body and "Test-InsideRoot $path (Get-Roots)" in body
    assert body.index("Test-InsideRoot") < body.index("Set-ProjectPair")


# ---------- служба: настоящий PowerShell на временной папке ----------

def _ps_run(tmp_path, body):
    """Подгружает функции и переменные поиска из tia-bridge.ps1 и выполняет body (без запуска службы и TIA)."""
    names = ("Read-Json", "Write-Json", "Get-Roots", "Test-Reparse", "Test-InsideRoot", "Find-Ap17", "Invoke-Scan", "Set-ProjectPair")
    script = tmp_path / "t.ps1"
    vars_ = [ln for ln in PS.splitlines() if re.match(r"^\$(DefaultRoots|SkipDirs|ScanMaxDepth|ScanMaxItems) =", ln)]
    script.write_text(
        "$ErrorActionPreference='Stop'\n$e=$null; $ast=[System.Management.Automation.Language.Parser]::ParseFile('" + str(PS_PATH) + "',[ref]$null,[ref]$e)\n"
        "$utf8 = New-Object System.Text.UTF8Encoding($false)\n"
        + "\n".join(vars_) + "\n"
        "foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and @('" + "','".join(names) + "') -contains $args[0].Name }, $true)) { Invoke-Expression $f.Extent.Text }\n"
        + body, encoding="utf-8-sig")
    subprocess.run([PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], check=True, timeout=120, capture_output=True)


@pytest.mark.skipif(not PWSH, reason="нет PowerShell")
def test_powershell_scan_depth_skips_and_atomic_file(tmp_path):
    bridge, rt = tmp_path / "bridge", tmp_path / "roots" / "TIAX"
    bridge.mkdir()
    files = [r"GWM\UB\UB.ap17", r"a\b\c\d\e\deep5.ap17",                     # внутри 5 уровней: находятся
             r"a\b\c\d\e\f\deep6.ap17", r"GWM\UB\IM\inner.ap17", r"GWM\UB\XRef\x.ap17", r"GWM\UB\System\s.ap17", r"GWM\UB\Logs\l.ap17",
             r"GWM\UB\Temp\t.ap17", r"GWM\UB\UB.ap17x", r"GWM\readme.txt"]    # глубже 5, служебные папки и не .ap17: пропускаются
    for f in files:
        p = rt / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (bridge / "tia-projects.json").write_text(json.dumps({"roots": [str(rt), str(tmp_path / "нет такой папки")], "projects": []}), encoding="utf-8")
    _ps_run(tmp_path, "$Root = '" + str(bridge) + "'\nInvoke-Scan\n")
    data = json.loads((bridge / "found-projects.json").read_text(encoding="utf-8-sig"))
    assert not list(bridge.glob("*.tmp"))                                         # запись атомарная: временного файла не осталось
    rels = sorted(i["rel"] for i in data["projects"])
    assert rels == sorted([r"GWM\UB\UB.ap17", r"a\b\c\d\e\deep5.ap17"])
    one = next(i for i in data["projects"] if i["rel"] == r"GWM\UB\UB.ap17")
    assert one["key"] == r"TIAX\GWM\UB\UB.ap17" and one["path"] == str(rt / "GWM" / "UB" / "UB.ap17") and one["size"] == 1 and one["modified"]
    assert data["time"] and data["roots"] == [str(rt), str(tmp_path / "нет такой папки")]


@pytest.mark.skipif(not PWSH, reason="нет PowerShell")
def test_powershell_default_roots_and_inside_root(tmp_path):
    bridge, rt, out = tmp_path / "bridge", tmp_path / "root", tmp_path / "out.json"
    bridge.mkdir()
    (rt / "p").mkdir(parents=True)
    (rt / "p" / "a.ap17").write_text("x", encoding="utf-8")
    (tmp_path / "outside.ap17").write_text("x", encoding="utf-8")
    (bridge / "tia-projects.json").write_text(json.dumps({"projects": [], "roots": [str(rt)]}), encoding="utf-8")
    _ps_run(tmp_path,
            "$Root = '" + str(bridge) + "'\n$roots = Get-Roots\n"
            "$res = [ordered]@{ roots = @($roots); inside = (Test-InsideRoot '" + str(rt / "p" / "a.ap17") + "' $roots);\n"
            "  dotdot = (Test-InsideRoot '" + str(rt / "p" / ".." / ".." / "outside.ap17") + "' $roots); outside = (Test-InsideRoot '" + str(tmp_path / "outside.ap17") + "' $roots);\n"
            "  sibling = (Test-InsideRoot '" + str(rt) + "2\\a.ap17' $roots); empty = (Test-InsideRoot '' $roots) }\n"
            "# без поля roots берутся значения по умолчанию\n"
            "Remove-Item -LiteralPath (Join-Path $Root 'tia-projects.json')\n$res['defaults'] = @(Get-Roots)\n"
            "[IO.File]::WriteAllText('" + str(out) + "', ($res | ConvertTo-Json -Depth 4), $utf8)\n")
    r = json.loads(out.read_text(encoding="utf-8-sig"))
    assert r["roots"] == [str(rt)] and r["inside"] is True
    assert r["dotdot"] is False and r["outside"] is False and r["sibling"] is False and r["empty"] is False
    assert r["defaults"] == ["C:\\TIA_for_claude_to_XML", "C:\\projects"]


@pytest.mark.skipif(not PWSH, reason="нет PowerShell")
def test_powershell_pair_is_written_and_replaced_with_history(tmp_path):
    bridge, out = tmp_path / "bridge", tmp_path / "out.json"
    bridge.mkdir()
    (bridge / "tia-projects.json").write_text(json.dumps({"roots": [r"C:\x"], "projects": [
        {"id": "wbs1", "name": "AE · WBS1", "plc": "AE_WBS_1", "path": r"C:\old\0.ap17"}]}), encoding="utf-8")
    body = ["$Root = '" + str(bridge) + "'"]
    for i in range(7):                                                   # семь замен пути: в history остаются пять последних
        body.append("Set-ProjectPair 'wbs1' 'AE_WBS_1' ([pscustomobject]@{ rel = 'ASTANA\\W1\\W1.ap17'; path = 'C:\\new\\" + str(i) + ".ap17' })")
    body.append("Set-ProjectPair 'gwm_ub' 'GWM_UB' ([pscustomobject]@{ rel = 'GWM\\UB\\UB.ap17'; path = 'C:\\TIA\\GWM\\UB\\UB.ap17' })")
    _ps_run(tmp_path, "\n".join(body) + "\n")
    cfg = json.loads((bridge / "tia-projects.json").read_text(encoding="utf-8-sig"))
    assert cfg["roots"] == [r"C:\x"]                                     # прочие поля файла сохраняются
    by = {p["id"]: p for p in cfg["projects"]}
    assert by["wbs1"]["path"] == r"C:\new\6.ap17" and by["wbs1"]["plc"] == "AE_WBS_1" and by["wbs1"]["name"] == "ASTANA · W1"
    assert by["wbs1"]["history"] == [rf"C:\new\{i}.ap17" for i in (5, 4, 3, 2, 1)]       # новые первыми, не более пяти
    assert by["gwm_ub"] == {"id": "gwm_ub", "name": "GWM · UB", "plc": "GWM_UB", "path": r"C:\TIA\GWM\UB\UB.ap17"}


# ---------- окно: чистая логика (node) и разметка ----------

def node(expr: str):
    code = f"const t=require({json.dumps(str(STATIC / 'tiaexport.js'))});console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout)


PLCS = [{"id": 1, "name": "GWM_UB"}, {"id": 7, "name": "AE_WBS_1"}]
PRJ = [{"id": "wbs1", "name": "AE · WBS1", "plc": "ae_wbs_1", "folder": "W1", "found_key": "K2"},
       {"id": "old", "name": "Старый", "plc": "OLD", "folder": "old", "found_key": None}]
FOUND = {"stale": False, "items": [{"key": "K1", "rel": r"GWM\UB\UB.ap17", "modified": "2026-10-20T07:03:00Z"},
                                   {"key": "K2", "rel": r"ASTANA\W1\W1.ap17", "modified": "2026-10-19T07:03:00Z"}]}


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_plc_and_project_lists():
    o = node(f"t.plcOptions({json.dumps(PLCS)}, {json.dumps(PRJ, ensure_ascii=False)}).map(x => [x.value, x.label])")
    assert o == [["GWM_UB", "GWM_UB"], ["AE_WBS_1", "AE_WBS_1 — проект: W1"]]                 # ПЛК без проекта выбирается, с проектом помечен папкой
    f = node(f"t.foundOptions({json.dumps(FOUND)}, null, x => '20.10 12:03').map(x => [x.value, x.label])")
    assert f[0] == ["K1", r"GWM\UB\UB.ap17 — изменён 20.10 12:03"] and len(f) == 2
    pair_out = node(f"t.foundOptions({json.dumps(FOUND)}, {json.dumps(PRJ[1], ensure_ascii=False)}, x => 'd').map(x => x.value)")
    assert pair_out == ["@pair", "K1", "K2"]                                                    # проект вне найденных папок не теряется
    pick = lambda proj, wanted: node(f"t.pickFound({json.dumps(FOUND)}, {json.dumps(proj, ensure_ascii=False)}, {json.dumps(wanted)})")
    assert pick(PRJ[0], "") == "K2" and pick(PRJ[0], "K1") == "K1" and pick(None, "") == "" and pick(None, "K9") == ""
    assert pick(PRJ[1], "") == "@pair" and pick(PRJ[1], "K1") == "K1"


@pytest.mark.skipif(not NODE, reason="нет node")
def test_js_confirm_hint_and_can_export():
    assert node(f"t.confirmLine('GWM_UB', {json.dumps(FOUND)}, null, 'K1')") == r"ПЛК GWM_UB ↔ проект GWM\UB\UB.ap17"
    assert node(f"t.confirmLine('OLD', {json.dumps(FOUND)}, {json.dumps(PRJ[1], ensure_ascii=False)}, '@pair')") == "ПЛК OLD ↔ проект old"
    assert node(f"[t.confirmLine('', {json.dumps(FOUND)}, null, 'K1'), t.confirmLine('A', {json.dumps(FOUND)}, null, '')]") == ["", ""]
    assert node(f"t.foundHint({json.dumps(FOUND)})") == ""
    assert "устарел" in node(f"t.foundHint({{...{json.dumps(FOUND)}, stale: true}})")
    empty = node("t.foundHint({items: [], stale: true})")
    assert "не нашла проекты в разрешённых папках" in empty and "roots" in empty and r"C:\tia-bridge\tia-projects.json" in empty and "администратор" in empty
    assert node("t.foundHint(null)") == empty
    run = {"job": {"state": "running"}}
    assert node(f"[t.canExport(true, 'A', 'K1', false, null), t.canExport(false, 'A', 'K1', false, null), t.canExport(true, '', 'K1', false, null),"
                f" t.canExport(true, 'A', '', false, null), t.canExport(true, 'A', 'K1', true, null), t.canExport(true, 'A', 'K1', false, {json.dumps(run)})]") == [
        True, False, False, False, False, False]


def test_window_markup_and_table_layout():
    for el in ("tia-src", "tia-prj", "tia-target", "tia-go"):
        assert re.search(rf'id="{el}"[^>]*title="[^"]{{40,}}"', JS), el
    assert "Проект (папка)" in JS and 'data-k="confirm"' in JS and 'data-k="found"' in JS
    # на сервер уходит имя ПЛК и ключ, не путь; «проект не задан» больше не блокирует выбор ПЛК
    assert "{ plc: plcName, found_key: choice, target_dir: target }" in JS and "disabled: true" not in JS
    assert "проект не задан" not in JS
    css = (STATIC / "tia.css").read_text(encoding="utf-8")
    assert re.search(r"\.tia-tbl td\.tia-plc\s*\{[^}]*white-space:\s*nowrap", css) and 'class="tia-plc"' in JS     # «GWM_UB» не переносится посреди слова
    assert "ПЛК ${p.plc}: " in JS and "ПЛК ${esc(p.plc)} · " in JS                                              # имя ПЛК в подсказках и в «Файле для ИИ»
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", css) and not re.search(r"font-size:\s*\d+px", css)
