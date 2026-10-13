"""Оповещения в Telegram: очередь сообщений (tg_outbox) и отправитель.

Шаг 1: схема, очередь, отправка с ограничителями частоты, статус для администратора.
Токен бота берётся только из переменной окружения TELEGRAM_BOT_TOKEN и нигде не сохраняется и не печатается.
Запись во внешнюю систему (Telegram) работает только при APP_ENV=prod.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import require_admin, require_user

log = logging.getLogger("tgnotify")
# httpx пишет адреса запросов (а в них токен) в журнал уровня INFO: глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

router = APIRouter(prefix="/api/telegram")

EVENT_TYPES = (
    "alarm_bit",            # авария из журнала
    "tag_alarm",            # переменная с флажком «Оповещение»
    "link_lost",            # потеря связи с ПЛК
    "link_restored",        # связь восстановлена
    "unlogged_downtime",    # простой не описан дольше N минут
    "interval_shortfall",   # интервал закрыт с недовыполнением
    "voltage_limit",        # напряжение вне порогов
    "shift_digest",         # итог смены
    "system",               # бэкап, место на диске, обновление
)
STATUSES = ("pending", "sent", "failed", "skipped_dev", "skipped_no_target")

DEDUPE_MIN = 10            # минут: окно сглаживания повторов
PER_CHAT_PER_MIN = 15      # сообщений в минуту в один чат
GLOBAL_PER_SEC = 25        # сообщений в секунду всего
COLLAPSE_AFTER = 10        # больше стольких сообщений одного типа в минуту в чат сворачиваются
MAX_RETRY_AFTER = 30.0     # дольше этого по 429 не ждём (секунд)
SEND_ATTEMPTS = 3
HTTP_TIMEOUT = 10.0
LOOP_INTERVAL = 2.0
BATCH_LIMIT = 50

# Одним INSERT для поллера (producer не вызывает API); параметры: $1 event_type, $2 text, $3 scope (json-строка), $4 dedupe_key
ENQUEUE_SQL = (
    "INSERT INTO tg_outbox (event_type, text, scope, dedupe_key) "
    "SELECT $1, $2, $3::jsonb, $4 "
    f"WHERE $4 = '' OR NOT EXISTS (SELECT 1 FROM tg_outbox WHERE event_type = $1 AND dedupe_key = $4 "
    f"AND created_at > now() - interval '{DEDUPE_MIN} minutes') "
    "RETURNING id"
)


def get_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def is_prod() -> bool:
    return os.getenv("APP_ENV", "dev").strip().lower() == "prod"


def plant_hour(now: datetime) -> int:
    off = float(os.getenv("KPI_UTC_OFFSET_HOURS", "5"))
    return (now.astimezone(timezone.utc) + timedelta(hours=off)).hour


async def ensure_tg_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_chats (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK (kind IN ('group','user')),
            user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
            is_general BOOLEAN NOT NULL DEFAULT FALSE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_subscriptions (
            id SERIAL PRIMARY KEY,
            chat_ref INTEGER NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            scope JSONB NOT NULL DEFAULT '{}',
            also_clear BOOLEAN NOT NULL DEFAULT FALSE,
            quiet_from SMALLINT,
            quiet_to SMALLINT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (chat_ref, event_type)
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_outbox (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            event_type TEXT NOT NULL,
            text TEXT NOT NULL,
            scope JSONB NOT NULL DEFAULT '{}',
            dedupe_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','skipped_dev','skipped_no_target')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            sent_at TIMESTAMPTZ
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS ix_tg_outbox_status_created ON tg_outbox (status, created_at)")


async def enqueue(pool: asyncpg.Pool, event_type: str, text: str, scope: dict | None = None, dedupe_key: str = "") -> int | None:
    """Кладёт сообщение в очередь; повтор с тем же dedupe_key за DEDUPE_MIN минут не пишется (возвращает None)."""
    return await pool.fetchval(ENQUEUE_SQL, event_type, text, json.dumps(scope or {}, ensure_ascii=False), dedupe_key or "")


# ---------- подбор адресатов ----------

def _as_dict(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


_SCOPE_KEYS = (("brands", "brand"), ("stations", "station"), ("classes", "class"), ("plc_ids", "plc_id"))


def scope_matches(sub_scope: Any, event_scope: Any) -> bool:
    """Пустой ключ области подписки = любые значения; заданный ключ: значение события должно входить в список."""
    sub, ev = _as_dict(sub_scope), _as_dict(event_scope)
    for sk, ek in _SCOPE_KEYS:
        allowed = sub.get(sk)
        if not allowed:
            continue
        if ek not in ev or ev[ek] is None:
            return False
        if str(ev[ek]) not in {str(x) for x in allowed}:
            return False
    return True


def in_quiet_hours(quiet_from: int | None, quiet_to: int | None, hour: int) -> bool:
    """Тихие часы [from, to) по плантовому времени; могут переходить через полночь; пустые или равные = нет тихих часов."""
    if quiet_from is None or quiet_to is None or quiet_from == quiet_to:
        return False
    if quiet_from < quiet_to:
        return quiet_from <= hour < quiet_to
    return hour >= quiet_from or hour < quiet_to


async def find_targets(pool: asyncpg.Pool, event_type: str, event_scope: Any, now: datetime) -> list[asyncpg.Record]:
    direct = _as_dict(event_scope).get("chat_ref")
    if direct is not None:  # пробное сообщение: прямо в один включённый чат, без подписок и тихих часов
        return await pool.fetch(
            "SELECT id, chat_id, title, '{}'::jsonb AS scope, NULL::smallint AS quiet_from, NULL::smallint AS quiet_to "
            "FROM tg_chats WHERE enabled AND id = $1",
            int(direct),
        )
    rows = await pool.fetch(
        "SELECT c.id, c.chat_id, c.title, s.scope, s.quiet_from, s.quiet_to "
        "FROM tg_chats c JOIN tg_subscriptions s ON s.chat_ref = c.id "
        "WHERE c.enabled AND s.enabled AND s.event_type = $1 ORDER BY c.id",
        event_type,
    )
    hour = plant_hour(now)
    return [r for r in rows if scope_matches(r["scope"], event_scope) and not in_quiet_hours(r["quiet_from"], r["quiet_to"], hour)]


# ---------- ограничители частоты ----------

class SlidingWindow:
    """Скользящее окно: не более limit событий за window секунд на ключ; время передаётся снаружи."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._ev: dict[Any, deque] = defaultdict(deque)

    def _trim(self, key: Any, now: float) -> deque:
        q = self._ev[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def count(self, key: Any, now: float) -> int:
        return len(self._trim(key, now))

    def wait_time(self, key: Any, now: float) -> float:
        """Сколько секунд ждать до свободного места (0 = можно сейчас)."""
        q = self._trim(key, now)
        if len(q) < self.limit:
            return 0.0
        return max(0.0, q[len(q) - self.limit] + self.window - now)

    def record(self, key: Any, now: float) -> None:
        self._trim(key, now).append(now)


# ---------- отправка ----------

class SendResult:
    def __init__(self, ok: bool, error: str = "", disable_chat: bool = False):
        self.ok, self.error, self.disable_chat = ok, error, disable_chat


def _clean(msg: str, token: str) -> str:
    return msg.replace(token, "***") if token else msg


async def send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    text: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_wait: float = MAX_RETRY_AFTER,
) -> SendResult:
    """Одно сообщение в чат (чистый текст, без parse_mode). 429: ждём retry_after; 5xx и сеть: до 3 попыток с нарастающей паузой;
    400/403: чат недоступен (бот удалён или заблокирован), его нужно отключить."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    err = ""
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            r = await client.post(url, json={"chat_id": chat_id, "text": text}, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            err = f"сеть: {type(e).__name__}"
            if attempt < SEND_ATTEMPTS:
                await sleep(float(2 ** (attempt - 1)))
            continue
        code = r.status_code
        if code == 200:
            return SendResult(True)
        try:
            body = r.json()
        except ValueError:
            body = {}
        desc = _clean(str(body.get("description", "")), token)[:200] if isinstance(body, dict) else ""
        if code in (400, 403):
            return SendResult(False, f"HTTP {code}: {desc}", disable_chat=True)
        if code == 429:
            params = body.get("parameters", {}) if isinstance(body, dict) else {}
            wait = float(params.get("retry_after", 1) or 1)
            err = f"HTTP 429: слишком часто, повтор через {wait:g} с"
            if wait > max_wait:
                return SendResult(False, err)
            if attempt < SEND_ATTEMPTS:
                await sleep(wait)
            continue
        err = f"HTTP {code}: {desc}"
        if code >= 500 and attempt < SEND_ATTEMPTS:
            await sleep(float(2 ** (attempt - 1)))
            continue
        if code < 500:
            return SendResult(False, err)
    return SendResult(False, err or "не удалось отправить")


class TgSender:
    def __init__(
        self,
        pool: asyncpg.Pool,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.pool, self.client, self.sleep, self.clock = pool, client, sleep, clock
        self.per_chat = SlidingWindow(PER_CHAT_PER_MIN, 60.0)
        self.global_win = SlidingWindow(GLOBAL_PER_SEC, 1.0)
        self.per_type = SlidingWindow(COLLAPSE_AFTER, 60.0)  # ключ (chat_id, event_type)

    async def _throttle(self, chat_id: int) -> None:
        """Ждёт, пока можно слать в этот чат и всего (не дольше разумного: ограничитель короткий)."""
        for _ in range(100):
            now = self.clock()
            w = max(self.per_chat.wait_time(chat_id, now), self.global_win.wait_time("all", now))
            if w <= 0:
                return
            await self.sleep(min(w, MAX_RETRY_AFTER))

    async def _deliver(self, http: httpx.AsyncClient, token: str, chat_id: int, text: str) -> SendResult:
        await self._throttle(chat_id)
        res = await send_message(http, token, chat_id, text, sleep=self.sleep)
        now = self.clock()
        self.per_chat.record(chat_id, now)
        self.global_win.record("all", now)
        return res

    async def process_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        rows = await self.pool.fetch(
            "SELECT id, event_type, text, scope FROM tg_outbox WHERE status = 'pending' ORDER BY created_at, id LIMIT $1", BATCH_LIMIT
        )
        if not rows:
            return 0
        token = get_token()
        prod = is_prod()
        own = self.client is None
        http = self.client or httpx.AsyncClient()
        overflow: dict[tuple[int, int, str], int] = defaultdict(int)  # (id чата, chat_id, тип) -> свёрнуто
        try:
            for row in rows:
                targets = await find_targets(self.pool, row["event_type"], row["scope"], now)
                if not targets:
                    await self._finish(row["id"], "skipped_no_target")
                    continue
                if not prod:
                    log.info("[dev, не отправлено] %s: %s", row["event_type"], row["text"])
                    await self._finish(row["id"], "skipped_dev")
                    continue
                if not token:
                    await self._finish(row["id"], "failed", "не задан TELEGRAM_BOT_TOKEN (переменная окружения / файл .env)", attempts=1)
                    continue
                delivered, errors = 0, []
                for t in targets:
                    key = (t["chat_id"], row["event_type"])
                    if self.per_type.count(key, self.clock()) >= COLLAPSE_AFTER:
                        overflow[(t["id"], t["chat_id"], row["event_type"])] += 1
                        delivered += 1
                        continue
                    self.per_type.record(key, self.clock())
                    res = await self._deliver(http, token, t["chat_id"], row["text"])
                    if res.ok:
                        delivered += 1
                        continue
                    errors.append(res.error)
                    if res.disable_chat:
                        await self.pool.execute("UPDATE tg_chats SET enabled = FALSE WHERE id = $1", t["id"])
                        log.warning("чат %s отключён: %s", t["chat_id"], res.error)
                if delivered:
                    await self._finish(row["id"], "sent", "; ".join(errors)[:500], attempts=1)
                else:
                    await self._finish(row["id"], "failed", "; ".join(errors)[:500], attempts=1)
            for (cref, chat_id, etype), n in overflow.items():
                res = await self._deliver(http, token, chat_id, f"… и ещё {n} аналогичных событий за минуту")
                if res.disable_chat:
                    await self.pool.execute("UPDATE tg_chats SET enabled = FALSE WHERE id = $1", cref)
        finally:
            if own:
                await http.aclose()
        return len(rows)

    async def _finish(self, row_id: int, status: str, error: str = "", attempts: int = 0) -> None:
        await self.pool.execute(
            "UPDATE tg_outbox SET status = $2, last_error = $3, attempts = attempts + $4, "
            "sent_at = CASE WHEN $2 = 'sent' THEN now() ELSE sent_at END WHERE id = $1",
            row_id, status, error, attempts,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # цикл не должен падать
                log.error("ошибка отправителя: %s", _clean(f"{type(e).__name__}: {e}", get_token()))
            try:
                await asyncio.wait_for(stop.wait(), timeout=LOOP_INTERVAL)
            except asyncio.TimeoutError:
                pass


# ---------- API ----------

@router.get("/status", summary="Состояние оповещений в Telegram (только администратор)")
async def tg_status(request: Request, user: dict = Depends(require_admin)):
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT status, count(*) AS n FROM tg_outbox WHERE created_at > now() - interval '24 hours' GROUP BY status"
    )
    queue = {s: 0 for s in STATUSES}
    for r in rows:
        queue[r["status"]] = r["n"]
    chats = await pool.fetchval("SELECT count(*) FROM tg_chats")
    return {
        "configured": bool(get_token()), "env": "prod" if is_prod() else "dev", "queue": queue, "chats": chats,
        "polling": {"running": POLLING["running"], "bot_username": bot_username()},
    }


# ---------- коды привязки (в памяти процесса) ----------

CODE_TTL_SEC = 900
CODE_LEN = 10
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без похожих символов: 0/O, 1/I/L
CODES: dict[str, dict] = {}


def create_code(kind: str, user_id: int | None, now: float | None = None) -> str:
    """Одноразовый код привязки на 15 минут. kind: 'user' (чат человека) или 'group' (общий чат, создаёт администратор)."""
    if kind not in ("user", "group"):
        raise ValueError("kind")
    now = time.time() if now is None else now
    for k in [k for k, v in CODES.items() if v["expires"] <= now]:
        del CODES[k]
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in CODES:
            break
    CODES[code] = {"kind": kind, "user_id": user_id, "expires": now + CODE_TTL_SEC}
    return code


def redeem_code(code: str, kind: str | None = None, now: float | None = None) -> dict | None:
    """Гасит код (повторно не сработает). Неверный, просроченный или другого вида (kind) код = None; код другого вида не гасится."""
    now = time.time() if now is None else now
    code = (code or "").strip().upper()
    entry = CODES.get(code)
    if entry is None:
        return None
    if entry["expires"] <= now:
        del CODES[code]
        return None
    if kind is not None and entry["kind"] != kind:
        return None
    del CODES[code]
    return entry


# ---------- обработчик входящих сообщений бота ----------

HELP_TEXT = (
    "Бот оповещений PMM.\n"
    "/start КОД - привязать этот чат к вашей учётной записи PMM (код даёт кнопка «Привязать Telegram»)\n"
    "/bind КОД - привязать группу как общий чат (только в группе, код создаёт администратор)\n"
    "/stop - отключить оповещения в этом чате\n"
    "/help - эта справка"
)
MSG_BAD_CODE = "Код не найден или устарел, запросите новый в PMM"
SendFn = Callable[[int, str], Awaitable[Any]]


def _tg_person(frm: dict) -> str:
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x).strip()
    uname = frm.get("username")
    return ((name + (f" (@{uname})" if uname else "")).strip() or "Telegram")[:200]


async def _bind_user_chat(pool: asyncpg.Pool, user_id: int, chat_id: int, title: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7331)")
            mine = await conn.fetchrow("SELECT id FROM tg_chats WHERE kind = 'user' AND user_id = $1 ORDER BY id LIMIT 1", user_id)
            same = await conn.fetchrow("SELECT id FROM tg_chats WHERE chat_id = $1", chat_id)
            if mine and same and mine["id"] != same["id"]:
                await conn.execute("DELETE FROM tg_chats WHERE id = $1", same["id"])
                same = None
            target = mine or same
            if target:
                await conn.execute(
                    "UPDATE tg_chats SET chat_id = $2, title = $3, kind = 'user', user_id = $4, is_general = FALSE, enabled = TRUE WHERE id = $1",
                    target["id"], chat_id, title, user_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO tg_chats (chat_id, title, kind, user_id) VALUES ($1, $2, 'user', $3)", chat_id, title, user_id
                )


async def handle_update(pool: asyncpg.Pool, update: dict, send: SendFn, now: float | None = None) -> None:
    """Обрабатывает одно обновление Telegram. send(chat_id, text) подставляется снаружи. В группах отвечает только на команды."""
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    is_group = chat.get("type") in ("group", "supergroup")
    text = (msg.get("text") or "").strip()
    is_cmd = text.startswith("/")
    if is_group and not is_cmd:
        return
    if not is_cmd:
        if text:
            await send(chat_id, "Не понимаю сообщение. Список команд: /help")
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/start":
        if is_group:
            await send(chat_id, "В группе используйте /bind КОД (код создаёт администратор в PMM). Справка: /help")
        elif not arg:
            await send(chat_id, "Откройте PMM, нажмите «Привязать Telegram» и перейдите по выданной ссылке. Справка: /help")
        else:
            entry = redeem_code(arg, "user", now)
            row = await pool.fetchrow("SELECT last_name, first_name FROM app_users WHERE id = $1", entry["user_id"]) if entry else None
            if not entry or not row:
                await send(chat_id, MSG_BAD_CODE)
            else:
                await _bind_user_chat(pool, entry["user_id"], chat_id, _tg_person(msg.get("from") or chat))
                await send(chat_id, f"Привязано к PMM: {row['last_name']} {row['first_name']}")
    elif cmd == "/bind":
        if not is_group:
            await send(chat_id, "Команда /bind работает только в группе: добавьте бота в группу и отправьте её там")
            return
        entry = redeem_code(arg, "group", now) if arg else None
        if not entry:
            await send(chat_id, MSG_BAD_CODE)
            return
        title = (chat.get("title") or "Группа")[:200]
        await pool.execute(
            "INSERT INTO tg_chats (chat_id, title, kind, user_id, is_general) VALUES ($1, $2, 'group', NULL, TRUE) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, kind = 'group', user_id = NULL, is_general = TRUE, enabled = TRUE",
            chat_id, title,
        )
        await send(chat_id, "Чат привязан как общий")
    elif cmd == "/stop":
        n = await pool.fetchval("WITH u AS (UPDATE tg_chats SET enabled = FALSE WHERE chat_id = $1 RETURNING 1) SELECT count(*) FROM u", chat_id)
        await send(chat_id, "Оповещения в этом чате отключены. Чтобы включить снова, привяжите чат заново в PMM" if n else "Этот чат не привязан к PMM")
    elif cmd == "/help":
        await send(chat_id, HELP_TEXT)
    else:
        await send(chat_id, "Неизвестная команда. Список команд: /help")


# ---------- опрос Telegram (long polling) ----------

POLLING: dict[str, Any] = {"running": False, "bot_username": None}
POLL_TIMEOUT = 25
BACKOFF_MAX = 60.0


def bot_username() -> str | None:
    return POLLING["bot_username"] or (os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or None)


class TgUpdatesPoller:
    def __init__(self, pool: asyncpg.Pool, client: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None):
        self.pool, self.client, self.sleep = pool, client, sleep
        self.offset: int | None = None

    async def _pause(self, stop: asyncio.Event, secs: float) -> None:
        if self.sleep is not None:
            await self.sleep(secs)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def _api(self, http: httpx.AsyncClient, token: str, method: str, payload: dict, timeout: float) -> httpx.Response:
        return await http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)

    async def run(self, stop: asyncio.Event) -> None:
        token = get_token()
        if not is_prod() or not token:
            log.info("опрос Telegram не запущен (%s)", "не APP_ENV=prod" if not is_prod() else "не задан TELEGRAM_BOT_TOKEN")
            return
        own = self.client is None
        http = self.client or httpx.AsyncClient()
        POLLING["running"] = True
        delay = 1.0
        try:
            if not POLLING["bot_username"]:
                try:
                    r = await self._api(http, token, "getMe", {}, HTTP_TIMEOUT)
                    if r.status_code == 200:
                        POLLING["bot_username"] = (r.json().get("result") or {}).get("username")
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("getMe не удался: %s", _clean(type(e).__name__, token))

            async def send(chat_id: int, text: str) -> None:
                res = await send_message(http, token, chat_id, text, sleep=self.sleep or asyncio.sleep)
                if not res.ok:
                    log.warning("ответ боту не отправлен: %s", _clean(res.error, token))

            while not stop.is_set():
                payload: dict = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
                if self.offset is not None:
                    payload["offset"] = self.offset
                try:
                    r = await self._api(http, token, "getUpdates", payload, POLL_TIMEOUT + 10)
                except httpx.HTTPError as e:
                    log.warning("опрос Telegram: ошибка сети (%s), пауза %g с", type(e).__name__, delay)
                    await self._pause(stop, delay)
                    delay = min(delay * 2, BACKOFF_MAX)
                    continue
                if r.status_code == 409:
                    log.error("опрос Telegram: этим же ботом уже управляет другой экземпляр (409); повтор через %g с", BACKOFF_MAX)
                    await self._pause(stop, BACKOFF_MAX)
                    continue
                if r.status_code != 200:
                    log.warning("опрос Telegram: HTTP %s, пауза %g с", r.status_code, delay)
                    await self._pause(stop, delay)
                    delay = min(delay * 2, BACKOFF_MAX)
                    continue
                delay = 1.0
                try:
                    updates = r.json().get("result") or []
                except ValueError:
                    updates = []
                for upd in updates:
                    try:
                        await handle_update(self.pool, upd, send)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("ошибка обработки сообщения бота: %s", _clean(f"{type(e).__name__}: {e}", token))
                    if isinstance(upd.get("update_id"), int):
                        self.offset = upd["update_id"] + 1  # подтверждаем только после обработки
        finally:
            POLLING["running"] = False
            if own:
                await http.aclose()


# ---------- API: привязка, подписки, чаты ----------

EVENT_CATALOG: dict[str, dict] = {
    "alarm_bit": {"title": "Авария", "description": "Сработала авария из журнала аварий ПЛК.",
                  "scope_fields": ["brands", "stations", "classes", "plc_ids"], "supports_clear": True, "default_enabled": True},
    "tag_alarm": {"title": "Оповещение по переменной", "description": "Сработала переменная с флажком «Оповещение».",
                  "scope_fields": ["brands", "stations", "plc_ids"], "supports_clear": True, "default_enabled": True},
    "link_lost": {"title": "Потеря связи с ПЛК", "description": "Контроллер перестал отвечать на опрос.",
                  "scope_fields": ["plc_ids"], "supports_clear": True, "default_enabled": True},
    "link_restored": {"title": "Связь с ПЛК восстановлена", "description": "Контроллер снова отвечает на опрос.",
                      "scope_fields": ["plc_ids"], "supports_clear": True, "default_enabled": True},
    "unlogged_downtime": {"title": "Простой не описан", "description": "Простой идёт дольше заданного времени, а причина не внесена.",
                          "scope_fields": ["brands", "stations"], "supports_clear": False, "default_enabled": False},
    "interval_shortfall": {"title": "Недовыполнение интервала", "description": "Интервал закрыт с фактом ниже плана.",
                           "scope_fields": ["brands", "stations"], "supports_clear": False, "default_enabled": False},
    "voltage_limit": {"title": "Напряжение вне порогов", "description": "Напряжение вышло за заданные пороги.",
                      "scope_fields": ["stations", "plc_ids"], "supports_clear": False, "default_enabled": False},
    "shift_digest": {"title": "Итог смены", "description": "Сводка плана, факта и простоев по окончании смены.",
                     "scope_fields": ["brands"], "supports_clear": False, "default_enabled": False},
    "system": {"title": "Системные события", "description": "Резервное копирование, место на диске, обновление системы.",
               "scope_fields": [], "supports_clear": False, "default_enabled": False},
}
assert set(EVENT_CATALOG) == set(EVENT_TYPES)
SCOPE_MAX_ITEMS = 50
SCOPE_MAX_LEN = 60


class LinkBody(BaseModel):
    kind: str = "user"


class SubItem(BaseModel):
    event_type: str
    enabled: bool = True
    scope: dict = Field(default_factory=dict)
    also_clear: bool = False
    quiet_from: int | None = None
    quiet_to: int | None = None


class SubsBody(BaseModel):
    chat_ref: int | None = None
    items: list[SubItem]


class ChatPatch(BaseModel):
    enabled: bool | None = None
    title: str | None = Field(default=None, max_length=200)


class TestBody(BaseModel):
    chat_ref: int | None = None


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _sub_out(r: asyncpg.Record) -> dict:
    return {"event_type": r["event_type"], "enabled": r["enabled"], "scope": _as_dict(r["scope"]),
            "also_clear": r["also_clear"], "quiet_from": r["quiet_from"], "quiet_to": r["quiet_to"]}


async def _subs_of(pool: asyncpg.Pool, chat_ref: int) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM tg_subscriptions WHERE chat_ref = $1 ORDER BY id", chat_ref)
    return [_sub_out(r) for r in rows]


async def _own_chat(pool: asyncpg.Pool, user: dict) -> asyncpg.Record | None:
    return await pool.fetchrow("SELECT * FROM tg_chats WHERE kind = 'user' AND user_id = $1 ORDER BY id LIMIT 1", user["id"])


async def _resolve_chat(pool: asyncpg.Pool, user: dict, chat_ref: int | None) -> asyncpg.Record:
    if chat_ref is None:
        row = await _own_chat(pool, user)
        if not row:
            raise HTTPException(status_code=404, detail="Telegram не привязан: нажмите «Привязать Telegram»")
        return row
    row = await pool.fetchrow("SELECT * FROM tg_chats WHERE id = $1", chat_ref)
    if not row:
        raise HTTPException(status_code=404, detail="Чат не найден")
    if user["role"] != "admin" and not (row["kind"] == "user" and row["user_id"] == user["id"]):
        raise HTTPException(status_code=403, detail="Чужой чат доступен только администратору")
    return row


def _clean_scope(event_type: str, scope: dict) -> dict:
    allowed = EVENT_CATALOG[event_type]["scope_fields"]
    out: dict[str, list[str]] = {}
    for k, v in scope.items():
        if k not in allowed:
            raise HTTPException(status_code=422, detail=f"Область «{k[:60]}» не подходит для события {event_type}")
        if not isinstance(v, list) or len(v) > SCOPE_MAX_ITEMS or not all(isinstance(x, str) and len(x) <= SCOPE_MAX_LEN for x in v):
            raise HTTPException(status_code=422, detail=f"Область «{k}»: список строк, до {SCOPE_MAX_ITEMS} элементов по {SCOPE_MAX_LEN} символов")
        if v:
            out[k] = v
    return out


@router.post("/link", summary="Код привязки Telegram (ссылка для чата пользователя или команда для общей группы)")
async def tg_link(body: LinkBody, user: dict = Depends(require_user)):
    if body.kind not in ("user", "group"):
        raise HTTPException(status_code=422, detail="kind: user или group")
    if body.kind == "group" and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Общий чат привязывает только администратор")
    code = create_code(body.kind, user["id"])
    name = bot_username()
    return {
        "code": code,
        "url": f"https://t.me/{name}?start={code}" if name and body.kind == "user" else None,
        "expires_in_sec": CODE_TTL_SEC,
        "bot_username": name,
        "command": f"/bind {code}" if body.kind == "group" else None,
        "message": None if name else "Имя бота не определено",
    }


@router.get("/me", summary="Привязан ли Telegram у текущего пользователя и его подписки")
async def tg_me(request: Request, user: dict = Depends(require_user)):
    pool = _pool(request)
    row = await _own_chat(pool, user)
    if not row:
        return {"linked": False, "chat": None, "subscriptions": []}
    return {"linked": True, "chat": {"id": row["id"], "title": row["title"], "enabled": row["enabled"]},
            "subscriptions": await _subs_of(pool, row["id"])}


@router.delete("/me", summary="Отвязать Telegram текущего пользователя")
async def tg_unlink(request: Request, user: dict = Depends(require_user)):
    pool = _pool(request)
    row = await _own_chat(pool, user)
    if row:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE tg_chats SET enabled = FALSE WHERE id = $1", row["id"])
                await conn.execute("DELETE FROM tg_subscriptions WHERE chat_ref = $1", row["id"])
    return {"linked": False}


@router.get("/events", summary="Каталог событий для подписок")
async def tg_events(user: dict = Depends(require_user)):
    return [{"event_type": t, **EVENT_CATALOG[t]} for t in EVENT_TYPES]


@router.get("/subscriptions", summary="Подписки своего чата (администратор: любого, ?chat_ref=)")
async def tg_subs_get(request: Request, chat_ref: int | None = None, user: dict = Depends(require_user)):
    pool = _pool(request)
    row = await _resolve_chat(pool, user, chat_ref)
    return {"chat_ref": row["id"], "items": await _subs_of(pool, row["id"])}


@router.put("/subscriptions", summary="Заменить весь набор подписок чата")
async def tg_subs_put(body: SubsBody, request: Request, user: dict = Depends(require_user)):
    pool = _pool(request)
    row = await _resolve_chat(pool, user, body.chat_ref)
    if len(body.items) > len(EVENT_TYPES):
        raise HTTPException(status_code=422, detail="Слишком много подписок")
    seen: set[str] = set()
    clean = []
    for it in body.items:
        if it.event_type not in EVENT_CATALOG:
            raise HTTPException(status_code=422, detail=f"Неизвестное событие: {it.event_type[:60]}")
        if it.event_type in seen:
            raise HTTPException(status_code=422, detail=f"Событие повторяется: {it.event_type}")
        seen.add(it.event_type)
        if (it.quiet_from is None) != (it.quiet_to is None):
            raise HTTPException(status_code=422, detail="Тихие часы: заполните оба поля или оставьте оба пустыми")
        for q in (it.quiet_from, it.quiet_to):
            if q is not None and not 0 <= q <= 23:
                raise HTTPException(status_code=422, detail="Тихие часы: целые числа 0..23")
        scope = _clean_scope(it.event_type, it.scope)
        clean.append((it, scope, it.also_clear and EVENT_CATALOG[it.event_type]["supports_clear"]))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM tg_subscriptions WHERE chat_ref = $1", row["id"])
            for it, scope, clear in clean:
                await conn.execute(
                    "INSERT INTO tg_subscriptions (chat_ref, event_type, scope, also_clear, quiet_from, quiet_to, enabled) "
                    "VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)",
                    row["id"], it.event_type, json.dumps(scope, ensure_ascii=False), clear, it.quiet_from, it.quiet_to, it.enabled,
                )
    return {"chat_ref": row["id"], "items": await _subs_of(pool, row["id"])}


@router.get("/chats", summary="Все привязанные чаты (только администратор)")
async def tg_chats(request: Request, _: dict = Depends(require_admin)):
    rows = await _pool(request).fetch(
        "SELECT c.id, c.chat_id, c.title, c.kind, c.is_general, c.enabled, c.user_id, u.last_name, u.first_name, "
        "(SELECT count(*) FROM tg_subscriptions s WHERE s.chat_ref = c.id) AS subs "
        "FROM tg_chats c LEFT JOIN app_users u ON u.id = c.user_id ORDER BY c.id"
    )
    return [{"id": r["id"], "chat_id": r["chat_id"], "title": r["title"], "kind": r["kind"], "is_general": r["is_general"],
             "enabled": r["enabled"], "user_id": r["user_id"],
             "user_name": f"{r['last_name']} {r['first_name']}" if r["user_id"] else None, "subscriptions": r["subs"]} for r in rows]


@router.put("/chats/{chat_ref}", summary="Включить/выключить чат или переименовать (только администратор)")
async def tg_chat_patch(chat_ref: int, body: ChatPatch, request: Request, _: dict = Depends(require_admin)):
    row = await _pool(request).fetchrow(
        "UPDATE tg_chats SET enabled = COALESCE($2, enabled), title = COALESCE($3, title) WHERE id = $1 RETURNING id, title, enabled",
        chat_ref, body.enabled, body.title,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return {"id": row["id"], "title": row["title"], "enabled": row["enabled"]}


@router.delete("/chats/{chat_ref}", summary="Удалить чат вместе с подписками (только администратор)")
async def tg_chat_delete(chat_ref: int, request: Request, _: dict = Depends(require_admin)):
    n = await _pool(request).fetchval("WITH d AS (DELETE FROM tg_chats WHERE id = $1 RETURNING 1) SELECT count(*) FROM d", chat_ref)
    if not n:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return {"deleted": chat_ref}


@router.post("/test", summary="Пробное сообщение «Проверка связи PMM» в свой чат (администратор: в любой)")
async def tg_test(body: TestBody, request: Request, user: dict = Depends(require_user)):
    pool = _pool(request)
    row = await _resolve_chat(pool, user, body.chat_ref)
    if not row["enabled"]:
        raise HTTPException(status_code=409, detail="Чат отключён")
    oid = await enqueue(pool, "system", "Проверка связи PMM", {"chat_ref": row["id"]})
    return {"queued": oid is not None, "id": oid, "env": "prod" if is_prod() else "dev"}
