"""Флажок «Включён в опрос» у ПЛК (plcs.is_active): поллер опрашивает только включённые; менять может только администратор."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
EP = "opc.tcp://10.0.0.1:4840"


async def _create(adm, name="A", **extra):
    r = await adm.post("/api/plcs", json={"name": name, "opc_endpoint": EP, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _active(client, plc_id):
    return {p["id"]: p["is_active"] for p in (await client.get("/api/plcs")).json()}[plc_id]


async def test_new_plc_is_active_by_default(users):
    pid = await _create(users["t_admin"])
    assert await _active(users["t_admin"], pid) is True


async def test_create_plc_switched_off(users):
    pid = await _create(users["t_admin"], is_active=False)
    assert await _active(users["t_admin"], pid) is False


async def test_put_switches_flag_and_returns_it(users):
    adm = users["t_admin"]
    pid = await _create(adm)
    r = await adm.put(f"/api/plcs/{pid}", json={"name": "A", "opc_endpoint": EP, "is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert await _active(adm, pid) is False
    r = await adm.put(f"/api/plcs/{pid}", json={"name": "A", "opc_endpoint": EP, "is_active": True})
    assert r.json()["is_active"] is True and await _active(adm, pid) is True


async def test_put_without_flag_keeps_value(users):
    """Старый клиент или правка имени не должны случайно включить выключенный ПЛК."""
    adm = users["t_admin"]
    pid = await _create(adm, is_active=False)
    r = await adm.put(f"/api/plcs/{pid}", json={"name": "B", "opc_endpoint": EP})
    assert r.status_code == 200 and await _active(adm, pid) is False


async def test_only_admin_changes_flag(users, anon):
    pid = await _create(users["t_admin"])
    body = {"name": "A", "opc_endpoint": EP, "is_active": False}
    assert (await anon.put(f"/api/plcs/{pid}", json=body)).status_code == 401
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        assert (await users[login].put(f"/api/plcs/{pid}", json=body)).status_code == 403, login
    assert await _active(users["t_admin"], pid) is True


def test_forms_have_checkbox_with_title_and_send_flag():
    for cid in ("new-plc-active", "edit-plc-active"):
        i = HTML.index(f'id="{cid}"')
        tag = HTML[HTML.rfind("<input", 0, i): HTML.index(">", i)]
        assert 'type="checkbox"' in tag and "title=" in tag, cid
    assert "is_active: document.getElementById('edit-plc-active').checked" in HTML
    assert "is_active: document.getElementById('new-plc-active').checked" in HTML


def test_switched_off_plc_is_greyed_in_list():
    assert "(item.is_active ? '' : ' off')" in HTML       # строка выключенного ПЛК получает класс off
    assert ".plc-dd-row.off .plc-dd-name" in HTML         # и приглушается стилем
