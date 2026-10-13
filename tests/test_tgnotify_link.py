"""Оповещения в Telegram, шаг 2: коды привязки, обработчик бота, опрос, API привязки и подписок.

Реальных обращений к api.telegram.org нет: клиент httpx работает через MockTransport."""
import asyncio
import json
import logging
import re

import httpx
import pytest

FAKE_TOKEN = "111222:TEST-ONLY-NOT-A-REAL-TOKEN"


def _t():
    import tgnotify
    return tgnotify


@pytest.fixture(autouse=True)
async def _env(monkeypatch, request):
    # приложение стартует до выставления prod (иначе его опрос полез бы в сеть)
    if {"app", "users", "anon"} & set(request.fixturenames):
        app = request.getfixturevalue("app")
        for name in ("tg_task", "tg_poll_task"):
            task = getattr(app.state, name, None)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    tg = _t()
    tg.CODES.clear()
    tg.POLLING.update(running=False, bot_username=None)
    yield
    tg.CODES.clear()
    tg.POLLING.update(running=False, bot_username=None)


class Sent:
    def __init__(self):
        self.items = []

    async def __call__(self, chat_id, text):
        self.items.append((chat_id, text))

    @property
    def last(self):
        return self.items[-1][1] if self.items else None


def upd(text, chat_id=500, chat_type="private", title=None, uid=1, frm=None):
    chat = {"id": chat_id, "type": chat_type}
    if title:
        chat["title"] = title
    return {"update_id": uid, "message": {"text": text, "chat": chat,
                                          "from": frm or {"first_name": "Иван", "last_name": "Петров", "username": "ivan_p"}}}


async def user_id(pool, login):
    return await pool.fetchval("SELECT id FROM app_users WHERE login = $1", login)


# ------------------------------------------------------------------ коды

def test_code_format_and_unique():
    tg = _t()
    codes = {tg.create_code("user", 1, now=0) for _ in range(50)}
    assert len(codes) == 50
    for c in codes:
        assert len(c) == 10 and re.fullmatch(r"[A-Z2-9]+", c) and not set(c) & set("OIL01")


def test_code_one_time_and_ttl():
    tg = _t()
    c = tg.create_code("user", 7, now=1000)
    e = tg.redeem_code(c, "user", now=1000 + 899)
    assert e["user_id"] == 7 and e["kind"] == "user"
    assert tg.redeem_code(c, "user", now=1000 + 899) is None  # одноразовый
    c2 = tg.create_code("user", 7, now=1000)
    assert tg.redeem_code(c2, "user", now=1000 + 900) is None  # 15 минут вышли
    assert tg.redeem_code(c2, "user", now=1000) is None  # просроченный удалён
    assert tg.redeem_code("WRONGCODE1", now=0) is None
    assert tg.redeem_code("", now=0) is None


def test_code_wrong_kind_not_consumed():
    tg = _t()
    c = tg.create_code("group", 1, now=0)
    assert tg.redeem_code(c, "user", now=1) is None
    assert tg.redeem_code(c.lower(), "group", now=1)["kind"] == "group"


# ------------------------------------------------------------------ handle_update

async def test_start_binds_user(users, pool):
    tg = _t()
    uid = await user_id(pool, "t_master")
    code = tg.create_code("user", uid)
    s = Sent()
    await tg.handle_update(pool, upd(f"/start {code}", chat_id=777), s)
    assert s.last == "Привязано к PMM: T_MASTER Тест"
    row = await pool.fetchrow("SELECT * FROM tg_chats WHERE chat_id = 777")
    assert row["kind"] == "user" and row["user_id"] == uid and row["enabled"] and not row["is_general"]
    assert row["title"] == "Иван Петров (@ivan_p)"
    await tg.handle_update(pool, upd(f"/start {code}", chat_id=778), s)  # код уже погашен
    assert s.last == "Код не найден или устарел, запросите новый в PMM"
    assert await pool.fetchval("SELECT count(*) FROM tg_chats") == 1


async def test_start_bad_code_and_no_code(users, pool):
    tg = _t()
    s = Sent()
    await tg.handle_update(pool, upd("/start ZZZZZZZZZZ"), s)
    assert s.last == "Код не найден или устарел, запросите новый в PMM"
    await tg.handle_update(pool, upd("/start"), s)
    assert "Привязать Telegram" in s.last
    assert await pool.fetchval("SELECT count(*) FROM tg_chats") == 0


async def test_start_rebind_updates_chat_id(users, pool):
    tg = _t()
    uid = await user_id(pool, "t_master")
    s = Sent()
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', uid)}", chat_id=801), s)
    first = await pool.fetchval("SELECT id FROM tg_chats WHERE chat_id = 801")
    await pool.execute("INSERT INTO tg_subscriptions (chat_ref, event_type) VALUES ($1, 'alarm_bit')", first)
    await pool.execute("UPDATE tg_chats SET enabled = FALSE WHERE id = $1", first)
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', uid)}", chat_id=802), s)
    rows = await pool.fetch("SELECT * FROM tg_chats")
    assert len(rows) == 1 and rows[0]["id"] == first and rows[0]["chat_id"] == 802 and rows[0]["enabled"]
    assert await pool.fetchval("SELECT count(*) FROM tg_subscriptions") == 1  # подписки сохранены


async def test_start_chat_moves_between_users(users, pool):
    tg = _t()
    a, b = await user_id(pool, "t_master"), await user_id(pool, "t_chief")
    s = Sent()
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', a)}", chat_id=810), s)
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', b)}", chat_id=810), s)
    rows = await pool.fetch("SELECT * FROM tg_chats")
    assert len(rows) == 1 and rows[0]["user_id"] == b


async def test_bind_group(users, pool):
    tg = _t()
    admin = await user_id(pool, "t_admin")
    s = Sent()
    code = tg.create_code("group", admin)
    await tg.handle_update(pool, upd(f"/bind {code}", chat_id=-100, chat_type="supergroup", title="Цех"), s)
    assert s.last == "Чат привязан как общий"
    row = await pool.fetchrow("SELECT * FROM tg_chats WHERE chat_id = -100")
    assert row["kind"] == "group" and row["is_general"] and row["title"] == "Цех" and row["user_id"] is None
    # неверный код в группе
    await tg.handle_update(pool, upd("/bind QQQQQQQQQQ", chat_id=-100, chat_type="group"), s)
    assert s.last == "Код не найден или устарел, запросите новый в PMM"
    # код пользователя для /bind не годится и не гасится
    ucode = tg.create_code("user", admin)
    await tg.handle_update(pool, upd(f"/bind {ucode}", chat_id=-101, chat_type="group"), s)
    assert s.last == "Код не найден или устарел, запросите новый в PMM"
    assert tg.redeem_code(ucode, "user") is not None
    # в личном чате /bind не работает
    code2 = tg.create_code("group", admin)
    await tg.handle_update(pool, upd(f"/bind {code2}", chat_id=900), s)
    assert "только в группе" in s.last
    assert tg.redeem_code(code2, "group") is not None


async def test_stop_help_unknown(users, pool):
    tg = _t()
    uid = await user_id(pool, "t_master")
    s = Sent()
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', uid)}", chat_id=820), s)
    await tg.handle_update(pool, upd("/stop", chat_id=820), s)
    assert "отключены" in s.last
    assert await pool.fetchval("SELECT enabled FROM tg_chats WHERE chat_id = 820") is False
    await tg.handle_update(pool, upd("/stop", chat_id=999), s)
    assert s.last == "Этот чат не привязан к PMM"
    await tg.handle_update(pool, upd("/help"), s)
    assert "/start КОД" in s.last and FAKE_TOKEN not in s.last
    await tg.handle_update(pool, upd("привет"), s)
    assert "/help" in s.last
    await tg.handle_update(pool, upd("/abracadabra"), s)
    assert "/help" in s.last


async def test_group_answers_only_commands(users, pool):
    tg = _t()
    s = Sent()
    await tg.handle_update(pool, upd("всем привет", chat_id=-5, chat_type="group"), s)
    assert s.items == []
    await tg.handle_update(pool, upd("/help@pmm_bot", chat_id=-5, chat_type="group"), s)
    assert len(s.items) == 1 and "/bind КОД" in s.last
    await tg.handle_update(pool, {"update_id": 1, "callback_query": {}}, s)  # прочие обновления игнорируются
    assert len(s.items) == 1


# ------------------------------------------------------------------ API: link, me

async def test_link_requires_login(anon):
    assert (await anon.post("/api/telegram/link", json={"kind": "user"})).status_code == 401
    assert (await anon.get("/api/telegram/me")).status_code == 401
    assert (await anon.get("/api/telegram/events")).status_code == 401


async def test_link_user_and_group(users, monkeypatch):
    tg = _t()
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@pmm_test_bot")
    r = await users["t_viewer"].post("/api/telegram/link", json={"kind": "user"})
    assert r.status_code == 200
    j = r.json()
    assert j["expires_in_sec"] == 900 and j["bot_username"] == "pmm_test_bot"
    assert j["url"] == f"https://t.me/pmm_test_bot?start={j['code']}" and j["code"] in tg.CODES
    assert FAKE_TOKEN not in r.text
    assert (await users["t_master"].post("/api/telegram/link", json={"kind": "group"})).status_code == 403
    rg = await users["t_admin"].post("/api/telegram/link", json={"kind": "group"})
    assert rg.status_code == 200 and tg.CODES[rg.json()["code"]]["kind"] == "group"
    assert rg.json()["command"] == f"/bind {rg.json()['code']}"
    assert (await users["t_admin"].post("/api/telegram/link", json={"kind": "x"})).status_code == 422


async def test_link_without_bot_name(users):
    r = await users["t_master"].post("/api/telegram/link", json={"kind": "user"})
    j = r.json()
    assert r.status_code == 200 and j["url"] is None and j["message"] == "Имя бота не определено" and j["code"]


async def test_me_and_unlink(users, pool):
    tg = _t()
    c = users["t_master"]
    assert (await c.get("/api/telegram/me")).json() == {"linked": False, "chat": None, "subscriptions": []}
    uid = await user_id(pool, "t_master")
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', uid)}", chat_id=830), Sent())
    r = await c.put("/api/telegram/subscriptions", json={"items": [{"event_type": "alarm_bit"}]})
    assert r.status_code == 200
    me = (await c.get("/api/telegram/me")).json()
    assert me["linked"] and me["chat"]["enabled"] and me["chat"]["title"] == "Иван Петров (@ivan_p)"
    assert [x["event_type"] for x in me["subscriptions"]] == ["alarm_bit"]
    assert (await c.delete("/api/telegram/me")).json() == {"linked": False}
    assert await pool.fetchval("SELECT enabled FROM tg_chats WHERE chat_id = 830") is False
    assert await pool.fetchval("SELECT count(*) FROM tg_subscriptions") == 0
    me = (await c.get("/api/telegram/me")).json()
    assert me["chat"]["enabled"] is False and me["subscriptions"] == []


# ------------------------------------------------------------------ API: events

async def test_events_catalog_full(users):
    r = await users["t_viewer"].get("/api/telegram/events")
    assert r.status_code == 200
    items = {e["event_type"]: e for e in r.json()}
    assert set(items) == set(_t().EVENT_TYPES)
    for e in items.values():
        assert e["title"] and e["description"] and isinstance(e["default_enabled"], bool) and isinstance(e["supports_clear"], bool)
        assert set(e["scope_fields"]) <= {"brands", "stations", "classes", "plc_ids"}
    assert items["alarm_bit"]["supports_clear"] and items["link_lost"]["supports_clear"] and items["tag_alarm"]["supports_clear"]
    assert not items["shift_digest"]["supports_clear"]


# ------------------------------------------------------------------ API: subscriptions

async def _link(pool, login, chat_id):
    tg = _t()
    uid = await user_id(pool, login)
    await tg.handle_update(pool, upd(f"/start {tg.create_code('user', uid)}", chat_id=chat_id), Sent())
    return await pool.fetchval("SELECT id FROM tg_chats WHERE chat_id = $1", chat_id)


async def test_subscriptions_replace_and_validate(users, pool):
    c = users["t_master"]
    assert (await c.get("/api/telegram/subscriptions")).status_code == 404  # не привязан
    await _link(pool, "t_master", 840)
    items = [
        {"event_type": "alarm_bit", "enabled": True, "scope": {"brands": ["GWM"], "stations": []}, "also_clear": True, "quiet_from": 22, "quiet_to": 6},
        {"event_type": "shift_digest", "also_clear": True},
    ]
    r = await c.put("/api/telegram/subscriptions", json={"items": items})
    assert r.status_code == 200, r.text
    got = {i["event_type"]: i for i in r.json()["items"]}
    assert got["alarm_bit"]["scope"] == {"brands": ["GWM"]} and got["alarm_bit"]["also_clear"] is True
    assert got["alarm_bit"]["quiet_from"] == 22 and got["alarm_bit"]["quiet_to"] == 6
    assert got["shift_digest"]["also_clear"] is False  # «и снятие» не поддерживается типом
    # полная замена набора
    r = await c.put("/api/telegram/subscriptions", json={"items": [{"event_type": "link_lost", "scope": {"plc_ids": ["1", "2"]}}]})
    assert [i["event_type"] for i in r.json()["items"]] == ["link_lost"]
    assert [i["event_type"] for i in (await c.get("/api/telegram/subscriptions")).json()["items"]] == ["link_lost"]
    # пустой набор очищает
    r = await c.put("/api/telegram/subscriptions", json={"items": []})
    assert r.json()["items"] == []

    async def bad(items, status=422):
        before = await pool.fetchval("SELECT count(*) FROM tg_subscriptions")
        r = await c.put("/api/telegram/subscriptions", json={"items": items})
        assert r.status_code == status, (items, r.text)
        assert await pool.fetchval("SELECT count(*) FROM tg_subscriptions") == before

    await bad([{"event_type": "nope"}])
    await bad([{"event_type": "alarm_bit"}, {"event_type": "alarm_bit"}])
    await bad([{"event_type": "alarm_bit", "scope": {"colors": ["red"]}}])
    await bad([{"event_type": "system", "scope": {"brands": ["GWM"]}}])  # ключ не из допустимых для типа
    await bad([{"event_type": "alarm_bit", "scope": {"brands": ["x"] * 51}}])
    await bad([{"event_type": "alarm_bit", "scope": {"brands": ["x" * 61]}}])
    await bad([{"event_type": "alarm_bit", "scope": {"brands": [5]}}])
    await bad([{"event_type": "alarm_bit", "scope": {"brands": "GWM"}}])
    await bad([{"event_type": "alarm_bit", "quiet_from": 22}])
    await bad([{"event_type": "alarm_bit", "quiet_from": 24, "quiet_to": 1}])
    await bad([{"event_type": "alarm_bit", "quiet_from": -1, "quiet_to": 1}])
    await bad([{"event_type": "alarm_bit", "quiet_from": 1.5, "quiet_to": 2}])
    await bad([{"event_type": "alarm_bit"}] * (len(_t().EVENT_TYPES) + 1))
    # неудачная замена не стирает прежний набор
    await c.put("/api/telegram/subscriptions", json={"items": [{"event_type": "system"}]})
    await bad([{"event_type": "alarm_bit"}, {"event_type": "bad"}])
    assert [i["event_type"] for i in (await c.get("/api/telegram/subscriptions")).json()["items"]] == ["system"]


async def test_subscriptions_access(users, pool):
    mine = await _link(pool, "t_master", 850)
    other = await _link(pool, "t_chief", 851)
    c = users["t_master"]
    assert (await c.get(f"/api/telegram/subscriptions?chat_ref={mine}")).status_code == 200
    assert (await c.get(f"/api/telegram/subscriptions?chat_ref={other}")).status_code == 403
    assert (await c.put("/api/telegram/subscriptions", json={"chat_ref": other, "items": []})).status_code == 403
    assert (await c.get("/api/telegram/subscriptions?chat_ref=99999")).status_code == 404
    # общий чат: обычный пользователь не может, администратор может
    admin = users["t_admin"]
    code = _t().create_code("group", await user_id(pool, "t_admin"))
    await _t().handle_update(pool, upd(f"/bind {code}", chat_id=-300, chat_type="group", title="Общий"), Sent())
    gid = await pool.fetchval("SELECT id FROM tg_chats WHERE chat_id = -300")
    assert (await c.get(f"/api/telegram/subscriptions?chat_ref={gid}")).status_code == 403
    assert (await c.put("/api/telegram/subscriptions", json={"chat_ref": gid, "items": []})).status_code == 403
    r = await admin.put("/api/telegram/subscriptions", json={"chat_ref": gid, "items": [{"event_type": "alarm_bit"}]})
    assert r.status_code == 200 and r.json()["chat_ref"] == gid
    assert (await admin.get(f"/api/telegram/subscriptions?chat_ref={other}")).status_code == 200


# ------------------------------------------------------------------ API: chats, test

async def test_chats_admin_only(users, pool):
    a = await _link(pool, "t_master", 860)
    await pool.execute("INSERT INTO tg_subscriptions (chat_ref, event_type) VALUES ($1, 'alarm_bit'), ($1, 'system')", a)
    assert (await users["t_master"].get("/api/telegram/chats")).status_code == 403
    assert (await users["t_master"].put(f"/api/telegram/chats/{a}", json={"enabled": False})).status_code == 403
    assert (await users["t_master"].delete(f"/api/telegram/chats/{a}")).status_code == 403
    r = await users["t_admin"].get("/api/telegram/chats")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["id"] == a and row["subscriptions"] == 2 and row["user_name"] == "T_MASTER Тест" and row["kind"] == "user"
    r = await users["t_admin"].put(f"/api/telegram/chats/{a}", json={"enabled": False, "title": "Мастер"})
    assert r.json() == {"id": a, "title": "Мастер", "enabled": False}
    assert (await users["t_admin"].put("/api/telegram/chats/99999", json={"enabled": True})).status_code == 404
    assert (await users["t_admin"].delete(f"/api/telegram/chats/{a}")).status_code == 200
    assert await pool.fetchval("SELECT count(*) FROM tg_chats") == 0
    assert await pool.fetchval("SELECT count(*) FROM tg_subscriptions") == 0
    assert (await users["t_admin"].delete(f"/api/telegram/chats/{a}")).status_code == 404


async def test_test_message_dev_skipped(users, pool):
    tg = _t()
    c = users["t_master"]
    assert (await c.post("/api/telegram/test", json={})).status_code == 404  # не привязан
    cid = await _link(pool, "t_master", 870)
    other = await _link(pool, "t_chief", 871)
    r = await c.post("/api/telegram/test", json={})
    assert r.status_code == 200 and r.json()["queued"]
    assert (await c.post("/api/telegram/test", json={"chat_ref": other})).status_code == 403
    sender = tg.TgSender(pool)
    await sender.process_once()
    row = await pool.fetchrow("SELECT * FROM tg_outbox WHERE id = $1", r.json()["id"])
    assert row["text"] == "Проверка связи PMM" and row["status"] == "skipped_dev"
    # админ шлёт в чужой чат
    ra = await users["t_admin"].post("/api/telegram/test", json={"chat_ref": other})
    assert ra.status_code == 200
    await sender.process_once()
    assert (await pool.fetchrow("SELECT status FROM tg_outbox WHERE id = $1", ra.json()["id"]))["status"] == "skipped_dev"


async def test_test_message_goes_only_to_that_chat_in_prod(users, pool, monkeypatch):
    tg = _t()
    mine = await _link(pool, "t_master", 880)
    await _link(pool, "t_chief", 881)
    await pool.execute("INSERT INTO tg_subscriptions (chat_ref, event_type) SELECT id, 'system' FROM tg_chats")
    monkeypatch.setenv("APP_ENV", "prod")
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    r = await users["t_master"].post("/api/telegram/test", json={})
    assert r.status_code == 200
    await tg.TgSender(pool, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))).process_once()
    assert sent == [{"chat_id": 880, "text": "Проверка связи PMM"}]


async def test_status_has_polling_without_token(users):
    tg = _t()
    tg.POLLING["bot_username"] = "pmm_bot"
    r = await users["t_admin"].get("/api/telegram/status")
    assert r.json()["polling"] == {"running": False, "bot_username": "pmm_bot"}
    assert FAKE_TOKEN not in r.text


# ------------------------------------------------------------------ опрос Telegram

class FakeTg:
    """Подставной Telegram для цикла опроса: сценарий по методам."""

    def __init__(self, stop, get_updates):
        self.stop, self.get_updates = stop, list(get_updates)
        self.calls = []

    def handler(self, req: httpx.Request):
        method = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "pmm_bot"}})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if method == "getUpdates":
            r = self.get_updates.pop(0)
            if not self.get_updates:
                self.stop.set()
            if isinstance(r, Exception):
                raise r
            return r if isinstance(r, httpx.Response) else httpx.Response(200, json={"ok": True, "result": r})
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _no_sleep(_):
    return None


async def test_poller_not_started_in_dev(pool, caplog):
    tg = _t()
    fake = FakeTg(asyncio.Event(), [[]])
    with caplog.at_level(logging.INFO, logger="tgnotify"):
        await tg.TgUpdatesPoller(pool, client=fake.client()).run(asyncio.Event())
    assert fake.calls == [] and tg.POLLING["running"] is False
    assert sum("опрос Telegram не запущен" in r.message for r in caplog.records) == 1


async def test_poller_not_started_without_token(pool, monkeypatch, caplog):
    tg = _t()
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    fake = FakeTg(asyncio.Event(), [[]])
    with caplog.at_level(logging.INFO, logger="tgnotify"):
        await tg.TgUpdatesPoller(pool, client=fake.client()).run(asyncio.Event())
    assert fake.calls == []
    assert sum("опрос Telegram не запущен" in r.message for r in caplog.records) == 1


async def test_poller_processes_batch_and_acks_offset(users, pool, monkeypatch, caplog):
    tg = _t()
    monkeypatch.setenv("APP_ENV", "prod")
    uid = await user_id(pool, "t_master")
    code = tg.create_code("user", uid)
    stop = asyncio.Event()
    fake = FakeTg(stop, [[upd(f"/start {code}", chat_id=900, uid=41), upd("/help", chat_id=900, uid=42)], []])
    with caplog.at_level(logging.DEBUG):
        await tg.TgUpdatesPoller(pool, client=fake.client(), sleep=_no_sleep).run(stop)
    gets = [b for m, b in fake.calls if m == "getUpdates"]
    assert "offset" not in gets[0] and gets[0]["timeout"] == 25 and gets[1]["offset"] == 43
    assert [m for m, _ in fake.calls].count("getMe") == 1
    texts = [b["text"] for m, b in fake.calls if m == "sendMessage"]
    assert texts[0] == "Привязано к PMM: T_MASTER Тест" and "/bind КОД" in texts[1]
    assert await pool.fetchval("SELECT user_id FROM tg_chats WHERE chat_id = 900") == uid
    assert tg.POLLING["bot_username"] == "pmm_bot" and tg.POLLING["running"] is False
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_TOKEN not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM tg_chats")], default=str)
    assert FAKE_TOKEN not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM tg_outbox")], default=str)


async def test_poller_does_not_ack_before_processing_and_survives_bad_update(users, pool, monkeypatch, caplog):
    tg = _t()
    monkeypatch.setenv("APP_ENV", "prod")
    stop = asyncio.Event()
    boom = {"update_id": 5, "message": {"text": "/stop", "chat": {"id": "не число", "type": "private"}}}
    fake = FakeTg(stop, [[boom, upd("/help", chat_id=901, uid=6)], []])
    with caplog.at_level(logging.INFO, logger="tgnotify"):
        await tg.TgUpdatesPoller(pool, client=fake.client(), sleep=_no_sleep).run(stop)
    gets = [b for m, b in fake.calls if m == "getUpdates"]
    assert gets[1]["offset"] == 7  # битое сообщение не блокирует очередь
    assert FAKE_TOKEN not in caplog.text


async def test_poller_survives_conflict_and_network_errors(pool, monkeypatch, caplog):
    tg = _t()
    monkeypatch.setenv("APP_ENV", "prod")
    stop = asyncio.Event()
    fake = FakeTg(stop, [httpx.Response(409, json={"ok": False, "description": "Conflict"}),
                         httpx.ConnectError("нет сети"), httpx.ConnectError("нет сети"),
                         httpx.Response(502), []])
    sleeps = []

    async def sl(s):
        sleeps.append(s)

    with caplog.at_level(logging.INFO, logger="tgnotify"):
        await tg.TgUpdatesPoller(pool, client=fake.client(), sleep=sl).run(stop)
    assert len([1 for m, _ in fake.calls if m == "getUpdates"]) == 5  # цикл дошёл до конца сценария
    assert sleeps[0] == 60.0  # 409: длинная пауза
    assert sleeps[1:4] == [1.0, 2.0, 4.0]  # сеть и 5xx: нарастающая пауза
    assert any("другой экземпляр" in r.message for r in caplog.records)
    assert FAKE_TOKEN not in caplog.text
