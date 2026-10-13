"""Порядок вкладок и списков (/api/ui/order): личный у вошедшего, общий по умолчанию задаёт администратор."""


async def test_anon_gets_shared_default_order(users, anon):
    r = await users["t_admin"].put("/api/ui/order", json={"key": "order-tabs", "order": ["b", "a"], "shared": True})
    assert r.status_code == 200
    r = await anon.get("/api/ui/order", params={"key": "order-tabs"})
    assert r.status_code == 200 and r.json() == {"order": ["b", "a"]}


async def test_empty_when_nothing_saved(anon):
    r = await anon.get("/api/ui/order", params={"key": "order-plc"})
    assert r.json() == {"order": []}


async def test_personal_overrides_shared_and_is_private(users, anon):
    await users["t_admin"].put("/api/ui/order", json={"key": "order-plc", "order": ["1", "2", "3"], "shared": True})
    r = await users["t_master"].put("/api/ui/order", json={"key": "order-plc", "order": ["3", "2", "1"]})
    assert r.status_code == 200
    assert (await users["t_master"].get("/api/ui/order", params={"key": "order-plc"})).json()["order"] == ["3", "2", "1"]
    assert (await users["t_viewer"].get("/api/ui/order", params={"key": "order-plc"})).json()["order"] == ["1", "2", "3"]
    assert (await anon.get("/api/ui/order", params={"key": "order-plc"})).json()["order"] == ["1", "2", "3"]


async def test_shared_only_admin(users, anon):
    body = {"key": "order-tabs", "order": ["a"], "shared": True}
    assert (await users["t_chief"].put("/api/ui/order", json=body)).status_code == 403
    assert (await anon.put("/api/ui/order", json=body)).status_code == 401
    assert (await users["t_admin"].put("/api/ui/order", json=body)).status_code == 200


async def test_anon_cannot_write_or_delete(anon):
    assert (await anon.put("/api/ui/order", json={"key": "order-tabs", "order": ["a"]})).status_code == 401
    assert (await anon.delete("/api/ui/order", params={"key": "order-tabs"})).status_code == 401


async def test_validation(users):
    c = users["t_master"]
    assert (await c.put("/api/ui/order", json={"key": "Bad Key!", "order": ["a"]})).status_code == 400
    assert (await c.get("/api/ui/order", params={"key": "Bad Key!"})).status_code == 400
    assert (await c.put("/api/ui/order", json={"key": "order-tabs", "order": ["x"] * 201})).status_code == 422
    assert (await c.put("/api/ui/order", json={"key": "order-tabs", "order": ["x" * 101]})).status_code == 422
    assert (await c.put("/api/ui/order", json={"key": "order-tabs", "order": [""]})).status_code == 422
    assert (await c.put("/api/ui/order", json={"key": "order-tabs", "order": "abc"})).status_code == 422
    assert (await c.put("/api/ui/order", json={"key": "order-tabs", "order": ["x"] * 200})).status_code == 200
    assert (await c.put("/api/ui/order", json={"key": "k" * 61, "order": ["a"]})).status_code == 400


async def test_delete_resets_personal_only(users):
    await users["t_admin"].put("/api/ui/order", json={"key": "order-alarms-brands", "order": ["AE"], "shared": True})
    c = users["t_master"]
    await c.put("/api/ui/order", json={"key": "order-alarms-brands", "order": ["GWM", "AE"]})
    assert (await c.delete("/api/ui/order", params={"key": "order-alarms-brands"})).status_code == 200
    assert (await c.get("/api/ui/order", params={"key": "order-alarms-brands"})).json()["order"] == ["AE"]
    assert (await c.delete("/api/ui/order", params={"key": "order-alarms-brands", "shared": "true"})).status_code == 403


async def test_repeated_put_replaces(users):
    c = users["t_master"]
    await c.put("/api/ui/order", json={"key": "order-andon-brands", "order": ["a", "b"]})
    await c.put("/api/ui/order", json={"key": "order-andon-brands", "order": ["b", "a", "c"]})
    assert (await c.get("/api/ui/order", params={"key": "order-andon-brands"})).json()["order"] == ["b", "a", "c"]


async def test_order_does_not_leak_into_col_widths(users):
    c = users["t_master"]
    await c.put("/api/ui/order", json={"key": "order-tabs", "order": ["a"]})
    r = await c.get("/api/ui/col-widths")
    assert r.status_code == 200  # ширины читаются как раньше; порядок живёт в той же таблице под своим ключом
