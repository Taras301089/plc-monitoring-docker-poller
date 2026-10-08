"""Порядок ПЛК в списке: общий для всех, меняет только администратор (кнопки «выше»/«ниже»)."""


async def _add(pool, *names):
    ids = []
    for n in names:
        ids.append(await pool.fetchval("INSERT INTO plcs (name, opc_endpoint) VALUES ($1, 'opc.tcp://10.0.0.1:4840') RETURNING id", n))
    return ids


async def _order(client):
    r = await client.get("/api/plcs")
    assert r.status_code == 200
    return [p["id"] for p in r.json()]


async def test_default_order_is_by_id(users, pool):
    a, b, c = await _add(pool, "A", "B", "C")
    assert await _order(users["t_admin"]) == [a, b, c]


async def test_move_down_and_up(users, pool):
    a, b, c = await _add(pool, "A", "B", "C")
    adm = users["t_admin"]
    r = await adm.put(f"/api/plcs/{a}/move", json={"direction": "down"})
    assert r.status_code == 200 and r.json()["order"] == [b, a, c]
    assert await _order(adm) == [b, a, c]
    r = await adm.put(f"/api/plcs/{c}/move", json={"direction": "up"})
    assert r.status_code == 200
    assert await _order(adm) == [b, c, a]


async def test_move_at_edges_changes_nothing(users, pool):
    a, b = await _add(pool, "A", "B")
    adm = users["t_admin"]
    assert (await adm.put(f"/api/plcs/{a}/move", json={"direction": "up"})).status_code == 200
    assert (await adm.put(f"/api/plcs/{b}/move", json={"direction": "down"})).status_code == 200
    assert await _order(adm) == [a, b]


async def test_order_is_shared_for_everyone(users, anon, pool):
    a, b = await _add(pool, "A", "B")
    await users["t_admin"].put(f"/api/plcs/{a}/move", json={"direction": "down"})
    assert await _order(anon) == [b, a]
    assert await _order(users["t_master"]) == [b, a]


async def test_only_admin_can_move(users, anon, pool):
    a, b = await _add(pool, "A", "B")
    assert (await anon.put(f"/api/plcs/{a}/move", json={"direction": "down"})).status_code == 401
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        r = await users[login].put(f"/api/plcs/{a}/move", json={"direction": "down"})
        assert r.status_code == 403, login
    assert await _order(users["t_admin"]) == [a, b]


async def test_bad_direction_and_unknown_plc(users, pool):
    (a,) = await _add(pool, "A")
    adm = users["t_admin"]
    assert (await adm.put(f"/api/plcs/{a}/move", json={"direction": "sideways"})).status_code == 422
    assert (await adm.put("/api/plcs/999999/move", json={"direction": "up"})).status_code == 404


async def test_new_plc_goes_to_the_end_and_delete_keeps_order(users, pool):
    a, b, c = await _add(pool, "A", "B", "C")
    adm = users["t_admin"]
    await adm.put(f"/api/plcs/{c}/move", json={"direction": "up"})          # A C B
    await adm.put(f"/api/plcs/{c}/move", json={"direction": "up"})          # C A B
    r = await adm.post("/api/plcs", json={"name": "D", "opc_endpoint": "opc.tcp://10.0.0.9:4840"})
    assert r.status_code == 200
    d = r.json()["id"]
    assert await _order(adm) == [c, a, b, d]
    assert (await adm.delete(f"/api/plcs/{a}")).status_code == 200
    assert await _order(adm) == [c, b, d]
