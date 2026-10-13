"""Оповещения в Telegram, шаг 1: схема, очередь, подбор адресатов, отправка (подставной транспорт), ограничители, статус.

Реальных обращений к api.telegram.org нет: клиент httpx работает через MockTransport."""
import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

FAKE_TOKEN = "111222:TEST-ONLY-NOT-A-REAL-TOKEN"
NOW = datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)  # плантовое 12:00 при смещении +5


def _t():
    import tgnotify
    return tgnotify


class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class Net:
    """Подставной Telegram: ответы по очереди, запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, httpx.Response) else httpx.Response(r[0], json=r[1])

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


OK = (200, {"ok": True, "result": {}})


@pytest.fixture(autouse=True)
async def _tg_env(monkeypatch, request):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("KPI_UTC_OFFSET_HOURS", "5")
    if "app" in request.fixturenames or "users" in request.fixturenames or "anon" in request.fixturenames:
        # фоновый отправитель приложения не должен перехватывать строки очереди во время теста
        app = request.getfixturevalue("app")
        task = getattr(app.state, "tg_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _chat(pool, chat_id, *, general=False, enabled=True, subs=()):
    cid = await pool.fetchval(
        "INSERT INTO tg_chats (chat_id, title, kind, is_general, enabled) VALUES ($1, $2, 'group', $3, $4) RETURNING id",
        chat_id, f"chat{chat_id}", general, enabled,
    )
    for s in subs:
        await sub(pool, cid, **s) if isinstance(s, dict) else await sub(pool, cid, s)
    return cid


async def sub(pool, cid, event_type="alarm_bit", scope=None, enabled=True, quiet_from=None, quiet_to=None):
    await pool.execute(
        "INSERT INTO tg_subscriptions (chat_ref, event_type, scope, enabled, quiet_from, quiet_to) VALUES ($1,$2,$3::jsonb,$4,$5,$6)",
        cid, event_type, json.dumps(scope or {}), enabled, quiet_from, quiet_to,
    )


async def _row(pool, rid):
    return await pool.fetchrow("SELECT * FROM tg_outbox WHERE id = $1", rid)


def _sender(pool, net, ft=None):
    ft = ft or FakeTime()
    return _t().TgSender(pool, client=net.client(), sleep=ft.sleep, clock=ft.clock), ft


# ------------------------------------------------------------------ схема и очередь
async def test_schema_idempotent(app, pool):
    t = _t()
    await t.ensure_tg_schema(pool)
    await t.ensure_tg_schema(pool)
    names = {r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"tg_chats", "tg_subscriptions", "tg_outbox"} <= names
    assert len(t.EVENT_TYPES) == 9  # в задаче названо «8», перечислено 9
    assert "alarm_bit" in t.EVENT_TYPES and "system" in t.EVENT_TYPES


async def test_enqueue_and_dedupe(app, pool):
    t = _t()
    a = await t.enqueue(pool, "link_lost", "нет связи", {"plc_id": 1}, dedupe_key="plc1")
    assert a
    assert await t.enqueue(pool, "link_lost", "нет связи ещё раз", {"plc_id": 1}, dedupe_key="plc1") is None
    assert await t.enqueue(pool, "link_restored", "другой тип", None, dedupe_key="plc1")  # другой тип не дубль
    assert await t.enqueue(pool, "link_lost", "без ключа 1")
    assert await t.enqueue(pool, "link_lost", "без ключа 2")  # пустой ключ не сглаживается
    assert await pool.fetchval("SELECT count(*) FROM tg_outbox") == 4
    # старше окна: не дубль
    await pool.execute("UPDATE tg_outbox SET created_at = now() - interval '11 minutes' WHERE dedupe_key='plc1'")
    assert await t.enqueue(pool, "link_lost", "снова", None, dedupe_key="plc1")
    st = await pool.fetchval("SELECT status FROM tg_outbox WHERE id=$1", a)
    assert st == "pending"


async def test_enqueue_sql_constant_for_poller(app, pool):
    t = _t()
    rid = await pool.fetchval(t.ENQUEUE_SQL, "system", "бэкап", json.dumps({}), "")
    assert rid


# ------------------------------------------------------------------ адресаты
async def test_targets_scope_and_flags(app, pool):
    t = _t()
    any_ = await _chat(pool, 1, subs=[{"event_type": "alarm_bit"}])
    br = await _chat(pool, 2, subs=[{"event_type": "alarm_bit", "scope": {"brands": ["GWM"]}}])
    other = await _chat(pool, 3, subs=[{"event_type": "alarm_bit", "scope": {"brands": ["Chery"], "stations": ["UB10"]}}])
    off_sub = await _chat(pool, 4, subs=[{"event_type": "alarm_bit", "enabled": False}])
    off_chat = await _chat(pool, 5, enabled=False, subs=[{"event_type": "alarm_bit"}])
    other_type = await _chat(pool, 6, subs=[{"event_type": "link_lost"}])
    general_no_sub = await _chat(pool, 7, general=True)
    general_sub = await _chat(pool, 8, general=True, subs=[{"event_type": "alarm_bit", "scope": {"classes": ["A"]}}])
    ev = {"brand": "GWM", "station": "UB10", "class": "A", "plc_id": 1}
    ids = {r["id"] for r in await t.find_targets(pool, "alarm_bit", ev, NOW)}
    assert ids == {any_, br, general_sub}
    ids = {r["id"] for r in await t.find_targets(pool, "alarm_bit", {"brand": "Chery", "station": "UB10"}, NOW)}
    assert ids == {any_, other}  # у general_sub задан класс, а у события его нет
    assert off_sub not in ids and off_chat not in ids and other_type not in ids and general_no_sub not in ids


def test_scope_matches_pure():
    t = _t()
    assert t.scope_matches({}, {})
    assert t.scope_matches({"plc_ids": [1, 2]}, {"plc_id": 2})
    assert not t.scope_matches({"plc_ids": [1, 2]}, {"plc_id": 3})
    assert not t.scope_matches({"stations": ["UB10"]}, {})
    assert t.scope_matches(json.dumps({"brands": ["GWM"]}), {"brand": "GWM"})


def test_quiet_hours_incl_midnight():
    q = _t().in_quiet_hours
    assert not q(None, None, 3)
    assert q(1, 6, 1) and q(1, 6, 5) and not q(1, 6, 6) and not q(1, 6, 0)
    assert q(22, 6, 23) and q(22, 6, 0) and q(22, 6, 5) and not q(22, 6, 6) and not q(22, 6, 21) and q(22, 6, 22)
    assert not q(5, 5, 5)


async def test_quiet_hours_target_filter(app, pool):
    t = _t()
    cid = await _chat(pool, 1)
    await sub(pool, cid, "alarm_bit", quiet_from=10, quiet_to=14)  # плантовое 12:00 внутри
    assert await t.find_targets(pool, "alarm_bit", {}, NOW) == []
    night = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)  # плантовое 23:00
    assert len(await t.find_targets(pool, "alarm_bit", {}, night)) == 1


# ------------------------------------------------------------------ отправка
async def test_no_target(app, pool):
    t = _t()
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net(OK)
    s, _ = _sender(pool, net)
    await s.process_once(NOW)
    assert (await _row(pool, rid))["status"] == "skipped_no_target"
    assert net.requests == []


async def test_dev_no_network(app, pool, monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "dev")
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "авария на UB10")
    net = Net(OK)
    s, _ = _sender(pool, net)
    with caplog.at_level(logging.INFO, logger="tgnotify"):
        await s.process_once(NOW)
    assert (await _row(pool, rid))["status"] == "skipped_dev"
    assert len(net.requests) == 0
    assert "авария на UB10" in caplog.text and FAKE_TOKEN not in caplog.text


async def test_prod_success_token_only_in_url(app, pool, caplog):
    t = _t()
    await _chat(pool, 555, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "Авария UB10 <b>тест</b>")
    net = Net(OK)
    s, _ = _sender(pool, net)
    with caplog.at_level(logging.DEBUG):
        await s.process_once(NOW)
    row = await _row(pool, rid)
    assert row["status"] == "sent" and row["sent_at"] and row["last_error"] == ""
    assert len(net.requests) == 1
    req = net.requests[0]
    assert str(req.url) == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body == {"chat_id": 555, "text": "Авария UB10 <b>тест</b>"}  # без parse_mode
    dump = json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM tg_outbox")], default=str)
    dump += json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM tg_chats")], default=str)
    assert FAKE_TOKEN not in dump and FAKE_TOKEN not in caplog.text


async def test_no_token(app, pool, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net(OK)
    s, _ = _sender(pool, net)
    await s.process_once(NOW)
    row = await _row(pool, rid)
    assert row["status"] == "failed" and "TELEGRAM_BOT_TOKEN" in row["last_error"]
    assert net.requests == []


async def test_429_retry_after(app, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net((429, {"ok": False, "parameters": {"retry_after": 3}}), OK)
    s, ft = _sender(pool, net)
    await s.process_once(NOW)
    assert (await _row(pool, rid))["status"] == "sent"
    assert len(net.requests) == 2 and 3.0 in ft.sleeps


async def test_429_too_long_wait_fails_without_blocking(app, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net((429, {"ok": False, "parameters": {"retry_after": 999}}))
    s, ft = _sender(pool, net)
    await s.process_once(NOW)
    assert (await _row(pool, rid))["status"] == "failed"
    assert max(ft.sleeps or [0]) < 60


async def test_5xx_retries_then_ok_and_then_fail(app, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    r1 = await t.enqueue(pool, "alarm_bit", "a")
    net = Net((502, {"ok": False}), httpx.ConnectError("boom"), OK)
    s, ft = _sender(pool, net)
    await s.process_once(NOW)
    assert (await _row(pool, r1))["status"] == "sent" and len(net.requests) == 3
    assert ft.sleeps[:2] == [1.0, 2.0]  # нарастающая пауза
    r2 = await t.enqueue(pool, "alarm_bit", "b")
    net2 = Net((500, {"ok": False}))
    s2, _ = _sender(pool, net2)
    await s2.process_once(NOW)
    row = await _row(pool, r2)
    assert row["status"] == "failed" and len(net2.requests) == 3 and "500" in row["last_error"]


async def test_403_disables_chat(app, pool):
    t = _t()
    cid = await _chat(pool, 9, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net((403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    s, _ = _sender(pool, net)
    await s.process_once(NOW)
    row = await _row(pool, rid)
    assert row["status"] == "failed" and "403" in row["last_error"] and "blocked" in row["last_error"]
    assert await pool.fetchval("SELECT enabled FROM tg_chats WHERE id=$1", cid) is False
    assert len(net.requests) == 1


async def test_network_error_text_has_no_token(app, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    rid = await t.enqueue(pool, "alarm_bit", "x")
    net = Net(httpx.ConnectError(f"fail https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"))
    s, _ = _sender(pool, net)
    await s.process_once(NOW)
    row = await _row(pool, rid)
    assert row["status"] == "failed" and FAKE_TOKEN not in row["last_error"]


# ------------------------------------------------------------------ ограничители и сворачивание
def test_sliding_window_pure():
    w = _t().SlidingWindow(3, 60.0)
    for i in range(3):
        assert w.wait_time("k", 100.0 + i) == 0
        w.record("k", 100.0 + i)
    assert w.wait_time("k", 103.0) == pytest.approx(57.0)  # освободится, когда уйдёт самое старое
    assert w.wait_time("other", 103.0) == 0
    assert w.wait_time("k", 160.5) == 0 and w.count("k", 160.5) == 2


async def test_rate_limit_per_chat_waits(app, pool, monkeypatch):
    t = _t()
    monkeypatch.setattr(t, "COLLAPSE_AFTER", 1000)
    await _chat(pool, 1, subs=["alarm_bit"])
    for i in range(16):
        await t.enqueue(pool, "alarm_bit", f"m{i}")
    net = Net(OK)
    s, ft = _sender(pool, net)
    s.per_type = t.SlidingWindow(1000, 60.0)
    await s.process_once(NOW)
    assert len(net.requests) == 16
    assert ft.sleeps and sum(ft.sleeps) >= 59  # 16-е сообщение дождалось окна в минуту
    times_ok = await pool.fetchval("SELECT count(*) FROM tg_outbox WHERE status='sent'")
    assert times_ok == 16


async def test_global_rate_limit(app, pool):
    t = _t()
    ft = FakeTime()
    s = t.TgSender(pool, client=Net(OK).client(), sleep=ft.sleep, clock=ft.clock)
    for _ in range(25):
        s.global_win.record("all", ft.t)
    assert s.global_win.wait_time("all", ft.t) > 0
    await s._throttle(1)
    assert s.global_win.wait_time("all", ft.t) == 0 and ft.sleeps


async def test_collapse_over_ten_per_minute(app, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    ids = [await t.enqueue(pool, "alarm_bit", f"авария {i}") for i in range(14)]
    net = Net(OK)
    s, _ = _sender(pool, net)
    await s.process_once(NOW)
    texts = [json.loads(r.content)["text"] for r in net.requests]
    assert len(texts) == 11  # 10 обычных и 1 итоговое
    assert texts[:10] == [f"авария {i}" for i in range(10)]
    assert texts[10] == "… и ещё 4 аналогичных событий за минуту"
    assert await pool.fetchval("SELECT count(*) FROM tg_outbox WHERE status='sent'") == 14


# ------------------------------------------------------------------ API
async def test_status_api(app, users, anon, pool):
    t = _t()
    await _chat(pool, 1, subs=["alarm_bit"])
    await t.enqueue(pool, "alarm_bit", "a")
    await pool.execute("INSERT INTO tg_outbox (event_type, text, status) VALUES ('system','b','skipped_dev')")
    r = await users["t_admin"].get("/api/telegram/status")
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] is True and d["env"] == "prod" and d["chats"] == 1
    assert d["queue"] == {"pending": 1, "sent": 0, "failed": 0, "skipped_dev": 1, "skipped_no_target": 0}
    assert FAKE_TOKEN not in r.text
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        assert (await users[login].get("/api/telegram/status")).status_code == 403
    assert (await anon.get("/api/telegram/status")).status_code == 401


async def test_status_not_configured(app, users, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    d = (await users["t_admin"].get("/api/telegram/status")).json()
    assert d["configured"] is False and d["env"] == "dev"
