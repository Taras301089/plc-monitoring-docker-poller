"""Вкладка «Тренды», блок «Запись переменных»: список архивных аналоговых, сохранение мёртвой зоны и порогов, права, экран."""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
_n = 0


async def _plc(pool, order=None):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active, sort_order) VALUES ($1, 'opc.tcp://10.0.0.1:4840', TRUE, $2) RETURNING id", f"tg{_n}", order)


async def _tag(pool, plc, name, ttype="ANALOG", archived=True):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plc_tags (plc_id, name, node_id, tag_type, is_archived) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        plc, name, f"ns=3;s=n{_n}", ttype, archived)


async def test_list_only_archived_analog_in_plc_order(users, pool):
    p2 = await _plc(pool, 2)
    p1 = await _plc(pool, 1)
    t_b = await _tag(pool, p2, "B")
    t_a = await _tag(pool, p1, "A", ttype="ANALOG")
    await _tag(pool, p1, "dig", ttype="DIGITAL")
    await _tag(pool, p1, "noarch", archived=False)
    r = await users["t_admin"].get("/api/trends/tags")
    assert r.status_code == 200
    rows = r.json()
    assert [x["tag_id"] for x in rows] == [t_a, t_b]
    assert set(rows[0]) >= {"plc_id", "plc_name", "tag_id", "name", "node_id", "deadband", "limit_low", "limit_high"}
    assert rows[0]["deadband"] is None


async def test_save_read_and_clear(users, pool):
    t = await _tag(pool, await _plc(pool, 1), "U")
    c = users["t_admin"]
    r = await c.put(f"/api/trends/tags/{t}", json={"deadband": 1, "limit_low": 207, "limit_high": 253})
    assert r.status_code == 200
    assert (r.json()["deadband"], r.json()["limit_low"], r.json()["limit_high"]) == (1, 207, 253)
    row = (await c.get("/api/trends/tags")).json()[0]
    assert (row["deadband"], row["limit_low"], row["limit_high"]) == (1, 207, 253)
    r = await c.put(f"/api/trends/tags/{t}", json={"deadband": None, "limit_low": None, "limit_high": None})
    assert r.status_code == 200
    row = await pool.fetchrow("SELECT deadband, limit_low, limit_high FROM plc_tags WHERE id = $1", t)
    assert (row["deadband"], row["limit_low"], row["limit_high"]) == (None, None, None)


@pytest.mark.parametrize("body,part", [
    ({"deadband": -1}, "отрицательной"),
    ({"limit_low": 253, "limit_high": 207}, "меньше верхнего"),
    ({"limit_low": 5, "limit_high": 5}, "меньше верхнего"),
    ({"limit_low": "abc"}, None),
])
async def test_validation(users, pool, body, part):
    t = await _tag(pool, await _plc(pool, 1), "U")
    r = await users["t_admin"].put(f"/api/trends/tags/{t}", json=body)
    assert r.status_code == 422
    if part:
        assert part in r.json()["detail"]


def test_validate_rejects_nan_inf():
    from fastapi import HTTPException
    from trends import validate_record_settings
    for args in ((float("nan"), None, None), (None, float("inf"), None), (None, None, float("-inf"))):
        with pytest.raises(HTTPException) as e:
            validate_record_settings(*args)
        assert e.value.status_code == 422
    validate_record_settings(None, None, None)
    validate_record_settings(0, 1, 2)


async def test_nan_string_in_json_rejected(users, pool):
    t = await _tag(pool, await _plc(pool, 1), "U")
    r = await users["t_admin"].put(f"/api/trends/tags/{t}", content='{"deadband": NaN}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422


async def test_not_found(users):
    r = await users["t_admin"].put("/api/trends/tags/99999999", json={"deadband": 1})
    assert r.status_code == 404


async def test_access(users, anon, pool):
    t = await _tag(pool, await _plc(pool, 1), "U")
    assert (await anon.get("/api/trends/tags")).status_code == 401
    assert (await anon.put(f"/api/trends/tags/{t}", json={"deadband": 1})).status_code == 401
    for login, c in users.items():
        if login == "t_admin":
            continue
        assert (await c.get("/api/trends/tags")).status_code == 403, login
        assert (await c.put(f"/api/trends/tags/{t}", json={"deadband": 1})).status_code == 403, login
    assert (await pool.fetchval("SELECT deadband FROM plc_tags WHERE id = $1", t)) is None
    assert (await users["t_admin"].get("/api/trends/tags")).status_code == 200
    assert (await users["t_admin"].put(f"/api/trends/tags/{t}", json={"deadband": 1})).status_code == 200


def test_screen_static():
    js = (STATIC / "trends.js").read_text(encoding="utf-8")
    assert "Запись переменных" in js
    assert 'id="tr-rec" hidden' in js and "box.hidden = !admin" in js
    assert "role === 'admin'" in js
    assert js.count("AbortSignal.timeout(6000)") >= 3
    assert "Отметьте переменные галочкой «Архив»" in js
    assert "Для напряжения" in js and "'207'" in js and "'253'" in js
    for col in ("Мёртвая зона", "Нижний порог", "Верхний порог", "Переменная", "ПЛК"):
        assert f">{col}</th>" in js
    # у каждой кнопки и поля ввода есть title
    import re
    for tag in re.findall(r"<(?:button|input)\b[^>]*>", js[js.index("function drawTags"):js.index("async function loadTags")]):
        assert "title=" in tag, tag
