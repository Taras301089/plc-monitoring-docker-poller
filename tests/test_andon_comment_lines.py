"""Andon, колонка «Причина»: каждый новый комментарий на своей строке (старые скрываются «+N», если не помещаются)."""
import re
from datetime import date
from pathlib import Path

import pytest

import dayflags
from test_responsibility import DAY, _item, _items, _put, _seed

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = (STATIC / "downtime.js").read_text(encoding="utf-8")
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dayflags, "plant_today", lambda: date(2026, 10, 12))


async def test_api_returns_recent_comments_oldest_first(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10)])
    item = (await _items(users["t_master"], scr))[0]
    for n in range(1, 8):      # 7 комментариев; в ответ идут последние 5, от старых к новым
        await pool.execute(
            "INSERT INTO kpi_downtime_comments (downtime_id, item_id, user_name, text, created_at) "
            "VALUES ($1, $2, 't_user', $3, now() + make_interval(secs => $4))", dt, item["id"], f"c{n}", float(n))
    got = (await _items(users["t_master"], scr))[0]
    assert [c["text"] for c in got["recent_comments"]] == ["c3", "c4", "c5", "c6", "c7"]
    assert got["last_comment"]["text"] == "c7"
    assert got["comments"] == 7            # общее число комментариев, чтобы показать «+N»


async def test_api_deleted_comments_are_not_listed(app, users, pool):
    scr, dt, r_def, _ = await _seed(pool)
    await _put(users["t_master"], dt, [_item(r_def, 10)])
    item = (await _items(users["t_master"], scr))[0]
    await pool.execute("INSERT INTO kpi_downtime_comments (downtime_id, item_id, user_name, text) VALUES ($1, $2, 'u', 'live')", dt, item["id"])
    await pool.execute(
        "INSERT INTO kpi_downtime_comments (downtime_id, item_id, user_name, text, deleted_at) VALUES ($1, $2, 'u', 'gone', now())", dt, item["id"])
    got = (await _items(users["t_master"], scr))[0]
    assert [c["text"] for c in got["recent_comments"]] == ["live"]


def test_andon_cell_renders_each_comment_on_its_own_line():
    assert "recent_comments" in JS
    assert "why-hid" in JS and ".why-hid" in CSS          # счётчик скрытых старых комментариев «+N»
    # комментарии выводятся в цикле, а не одним последним
    assert re.search(r"for \(const cm of", JS) or "shownComments" in JS
