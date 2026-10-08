"""Статус опроса ПЛК рядом с ПЛК: связь ЕСТЬ только при свежем успешном опросе; цвет точки отражает связь, а не флаг is_active."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")
EP = "opc.tcp://10.0.0.1:4840"


async def _plc(pool, name, active=True, status=None):
    """status: None — нет записи статуса; иначе (is_connected, секунд с последнего успешного опроса или None, секунд с обновления статуса, ошибка)."""
    pid = await pool.fetchval("INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, $2, $3) RETURNING id", name, EP, active)
    if status:
        connected, poll_ago, upd_ago, err = status
        await pool.execute(
            "INSERT INTO plc_connection_status (plc_id, is_connected, last_successful_poll, last_error, updated_at) "
            "VALUES ($1, $2, CASE WHEN $3::int IS NULL THEN NULL ELSE now() - make_interval(secs => $3::int) END, $4, now() - make_interval(secs => $5::int))",
            pid, connected, poll_ago, err, upd_ago,
        )
    return pid


async def _item(client, pid):
    return {p["id"]: p for p in (await client.get("/api/plcs")).json()}[pid]


async def test_connected_and_fresh_is_ok(users, pool):
    pid = await _plc(pool, "A", status=(True, 5, 5, ""))
    it = await _item(users["t_admin"], pid)
    assert it["link_state"] == "ok"
    assert 4 <= it["poll_age_sec"] <= 15


async def test_not_connected_shows_error_and_time(users, pool):
    pid = await _plc(pool, "A", status=(False, 600, 20, "BadUserAccessDenied"))
    it = await _item(users["t_admin"], pid)
    assert it["link_state"] == "no_link"
    assert "BadUserAccessDenied" in it["link_error"]
    assert 595 <= it["poll_age_sec"] <= 620


async def test_never_connected_has_no_poll_time(users, pool):
    pid = await _plc(pool, "A", status=(False, None, 20, "OPC UA connection is unavailable"))
    it = await _item(users["t_admin"], pid)
    assert it["link_state"] == "no_link" and it["poll_age_sec"] is None


async def test_stale_status_is_not_a_connection(users, pool):
    """Поллер остановлен: в базе осталось «подключён», но статус давно не обновлялся — связи ЕСТЬ показывать нельзя."""
    pid = await _plc(pool, "A", status=(True, 3600, 3600, ""))
    assert (await _item(users["t_admin"], pid))["link_state"] == "stale"


async def test_no_status_row_is_unknown(users, pool):
    pid = await _plc(pool, "A", status=None)
    assert (await _item(users["t_admin"], pid))["link_state"] == "unknown"


async def test_switched_off_plc_is_off_even_if_connected(users, pool):
    pid = await _plc(pool, "A", active=False, status=(True, 5, 5, ""))
    assert (await _item(users["t_admin"], pid))["link_state"] == "off"


async def test_status_visible_without_login(users, anon, pool):
    pid = await _plc(pool, "A", status=(True, 5, 5, ""))
    assert (await _item(anon, pid))["link_state"] == "ok"


def test_ui_shows_status_with_polling_and_timeout():
    assert "link_state" in HTML and "scan_age_sec" in HTML
    assert "назад" in HTML and "не сканировался" in HTML
    assert "refreshPlcStatus" in HTML
    assert "AbortSignal.timeout(6000)" in HTML[HTML.index("async function refreshPlcStatus"):]
    assert "setInterval(refreshPlcStatus, 5000)" in HTML


def test_ui_dot_only_for_real_link():
    body = HTML[HTML.index("function plcStatusHtml"): HTML.index("// Статус опроса обновляется сам")]
    assert "'on'" in body and "'off'" in body and "plc-dot" in body    # зелёная и красная точка по состоянию связи
    assert "is_active" not in body                                   # флаг включения точкой не показывается
    assert "case 'off'" in body and "выключен" in body               # у выключенного ПЛК надпись без точки


def test_refresh_does_not_reload_variables():
    body = HTML[HTML.index("async function refreshPlcStatus"): HTML.index("function renderPlcDropdown")]
    assert "loadVariables" not in body and "loadPlcs" not in body


def test_text_does_not_repeat_what_the_dot_says():
    """Точка говорит о связи; справа только давность последнего сканирования переменных; причина ошибки в подсказке точки."""
    body = HTML[HTML.index("function plcStatusHtml"): HTML.index("// Статус опроса обновляется сам")]
    assert "`нет связи" not in body and "'нет связи" not in body
    assert "`опрос " not in body and "'опрос " not in body
    assert "обновлено ${" not in body                       # «обновлено N с назад» (возраст опроса) убрано
    assert "poll_age_sec" not in body                       # возраст опроса не показывается нигде
    assert "scan_age_sec" in body and "не сканировался" in body
    assert "item.link_error" in body                        # причина ошибки есть (в подсказке)


async def test_scan_age_for_list(users, pool):
    """Давность последнего сканирования переменных («Обновить с ПЛК») отдаётся по каждому ПЛК; нет сканирований: None."""
    scanned = await _plc(pool, "A", status=(True, 5, 5, ""))
    never = await _plc(pool, "B", status=(True, 5, 5, ""))
    await pool.execute(
        "INSERT INTO plc_discovered_nodes (plc_id, db_name, variable_name, node_id, browse_path, updated_at) "
        "VALUES ($1, 'DB', 'v', 'ns=3;s=v', 'DB.v', now() - interval '7 days')", scanned)
    import main
    main._scan_cache["at"] = 0   # давность сканирования кэшируется на 30 с; в тесте сбрасываем
    adm = users["t_admin"]
    a, b = await _item(adm, scanned), await _item(adm, never)
    assert 7 * 86400 - 60 <= a["scan_age_sec"] <= 7 * 86400 + 60
    assert b["scan_age_sec"] is None
