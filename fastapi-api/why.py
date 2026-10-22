"""Ответы киповцам «почему стоит оборудование» и «что за авария» (S-8): логика ПЛК из tia_logic + живые значения (только чтение).

Тип А: explain_output(plc_id, имя_выхода) раскрывает условия включения выхода (И/ИЛИ/НЕ/сравнения, вызовы блоков на 2 уровня),
одним запросом читает значения всех сигналов через поллер, вычисляет и называет, чего не хватает.
Тип Б: explain_alarm(plc_id, номер_бита) находит в логике ГДЕ формируется авария и что её держит сейчас.
Нераспознанное (SCL, строковые функции, фронты) не угадывается: состояние unknown и пометка «часть логики не разобрана».
Простые названия сигналов (signal_names) и справка по авариям (alarm_help) берутся из БД, если таблицы есть (задача S-12), иначе
показывается техническое имя с пометкой «для инженера» и «инструкции пока нет». Таблиц модуль не создаёт и в ПЛК ничего не пишет.

Для тестов все источники подставляются параметрами: values (значения сигналов), since (секунды «уже так»), names (простые названия),
catalog / help / events (данные аварии), logic (разобранная логика)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg
import httpx
from fastapi import APIRouter, Depends, Query, Request

import tia_logic
from auth import require_user
from tia_logic import norm_name, split_name, to_text

router = APIRouter(prefix="/api/why", tags=["why"])

POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")
READ_TIMEOUT = 6.0            # тайм-аут чтения значений и всего ответа, секунды
MAX_LINES = 6                 # длина ответа киповцу
MAX_SHOWN = 2                 # сколько причин показывать, остальное «ещё N»
CALL_DEPTH = 2                # на сколько уровней раскрывать вызовы блоков
MSG_BLOCKS = ("02Msg_Stn", "01Msg_Cell")                 # блоки сборки аварий GWM UB (FC1401, FC1400)
ALARM_ADDR = re.compile(r"MSG\.W\[(\d+)\]\.x(\d+)", re.I)  # адрес аварии в заголовке сети вызова блока
SKIP_SIGNALS = ("sys_allways",)                           # постоянно включённые служебные сигналы не называем причиной
HISTORY_DAYS = 7
MISSING = object()


# ------------------------------------------------------------------ чтение логики и значений
_LOGIC_CACHE: dict[str, tuple[float, tia_logic.TiaLogic]] = {}


def _bridge_dir() -> Path:
    return Path(os.environ.get("TIA_BRIDGE_DIR", "/tia-bridge"))


def load_logic_file(project_id: str) -> tia_logic.TiaLogic | None:
    """Логика проекта из <мост>/logic/<id>.json; перечитывается при смене файла."""
    f = _bridge_dir() / "logic" / f"{project_id}.json"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return None
    hit = _LOGIC_CACHE.get(project_id)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        lg = tia_logic.TiaLogic.from_json(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None
    _LOGIC_CACHE[project_id] = (mt, lg)
    return lg


async def logic_for_plc(pool: asyncpg.Pool, plc_id: int) -> tia_logic.TiaLogic | None:
    """Проект TIA, связанный с ПЛК по имени (tia-projects.json, поле plc)."""
    import tia
    name = await pool.fetchval("SELECT name FROM plcs WHERE id = $1", plc_id)
    if not name:
        return None
    for p in tia.load_projects():
        if p.get("plc") == name:
            return load_logic_file(p["id"])
    return None


def node_id_for(name: str) -> str:
    """Запасной адрес узла OPC UA из имени TIA: DB."Структура"."Поле" -> ns=3;s="DB"."Структура"."Поле"."""
    out = []
    for part in split_name(name):
        m = re.match(r"^(.*?)((?:\[[^\]]*\])*)$", part)
        out.append(f'"{m.group(1)}"{m.group(2)}')
    return "ns=3;s=" + ".".join(out)


def _name_variants(name: str) -> list[str]:
    return list(dict.fromkeys([name, ".".join(split_name(name))]))


async def _tags_for(pool: asyncpg.Pool, plc_id: int, names: list[str]) -> dict[str, dict[str, Any]]:
    """norm_name -> {id, node_id, type} из plc_tags (по имени тега или адресу узла)."""
    want = {norm_name(n) for n in names}
    cand = [v for n in names for v in _name_variants(n)]
    nodes = [node_id_for(n) for n in names]
    rows = await pool.fetch(
        "SELECT id, name, node_id, tag_type FROM plc_tags WHERE plc_id = $1 AND (name = ANY($2::text[]) OR node_id = ANY($3::text[]))",
        plc_id, cand, nodes)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = norm_name(r["name"])
        if k not in want:
            k = next((norm_name(n) for n in names if node_id_for(n) == r["node_id"]), "")
        if k in want and k not in out:
            out[k] = {"id": r["id"], "node_id": r["node_id"], "type": r["tag_type"]}
    return out


async def _poller_read(plc_id: int, node_ids: list[str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=3.0)) as client:
        r = await client.post(f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/values", json={"node_ids": node_ids})
    r.raise_for_status()
    return r.json().get("values", {})


async def read_live(pool: asyncpg.Pool | None, plc_id: int, names: list[str], *,
                    reader: Callable[[int, list[str]], Awaitable[dict[str, Any]]] | None = None,
                    timeout: float = READ_TIMEOUT) -> tuple[dict[str, Any], dict[str, int], str | None]:
    """Живые значения всех сигналов ОДНИМ запросом к поллеру (только чтение): (norm_name -> значение, norm_name -> tag_id, ошибка).
    Нет значения у сигнала: его нет в словаре. Любой сбой связи даёт пустой словарь и текст ошибки."""
    tags = await _tags_for(pool, plc_id, names) if pool is not None else {}
    nid = {norm_name(n): (tags.get(norm_name(n)) or {}).get("node_id") or node_id_for(n) for n in names}
    tag_ids = {k: t["id"] for k, t in tags.items()}
    try:
        raw = await asyncio.wait_for((reader or _poller_read)(plc_id, list(dict.fromkeys(nid.values()))), timeout=timeout)
    except Exception as exc:  # noqa: BLE001  нет связи, тайм-аут, ПЛК не активен
        return {}, tag_ids, "нет связи с ПЛК" if not isinstance(exc, asyncio.TimeoutError) else "ПЛК не ответил за %d с" % timeout
    vals: dict[str, Any] = {}
    for k, n in nid.items():
        item = raw.get(n) if isinstance(raw, dict) else None
        if isinstance(item, dict) and item.get("ok") and isinstance(item.get("v"), (bool, int, float)):
            vals[k] = item["v"]
    return vals, tag_ids, None


async def load_since(pool: asyncpg.Pool, tag_ids: dict[str, int], vals: dict[str, Any], now: datetime) -> dict[str, int]:
    """Сколько секунд сигнал в текущем значении: последнее изменение в digital_telemetry (пишется при смене), если оно совпадает с живым
    значением. Нет в архиве или нет совпадения: сигнала нет в ответе («время неизвестно»)."""
    if not tag_ids:
        return {}
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (tag_id) tag_id, ts, value FROM digital_telemetry WHERE tag_id = ANY($1::int[]) ORDER BY tag_id, ts DESC",
            list(tag_ids.values()))
    except asyncpg.PostgresError:
        return {}
    by_id = {r["tag_id"]: r for r in rows}
    out: dict[str, int] = {}
    for k, tid in tag_ids.items():
        r = by_id.get(tid)
        if r is not None and k in vals and bool(vals[k]) == bool(r["value"]):
            out[k] = max(0, int((now - r["ts"]).total_seconds()))
    return out


async def load_names(pool: asyncpg.Pool, plc_id: int) -> dict[str, tuple[str, str]]:
    """norm_name -> (простое название, где смотреть) из signal_names (S-12); таблицы или записей нет: пустой словарь."""
    try:
        rows = await pool.fetch("SELECT tag_key, simple_name, place FROM signal_names WHERE plc_id = $1 AND simple_name <> ''", plc_id)
    except asyncpg.PostgresError:
        return {}
    return {r["tag_key"]: (r["simple_name"], r["place"]) for r in rows}


def friendly(names: dict[str, tuple[str, str]], tag: str | None) -> tuple[str, str, bool]:
    """(название, где смотреть, есть ли простое название). Нет простого названия: «сигнал <тех. имя>» для инженера."""
    if not tag:
        return "часть условий", "", False
    hit = names.get(norm_name(tag))
    if hit and hit[0]:
        return hit[0], hit[1] or "", True
    return f"сигнал {tag}", "", False


# ------------------------------------------------------------------ вычисление дерева условий
def _flat(node: Any) -> Any:
    """Вызовы блоков (раскрытые chains_for) заменяются условием их единственной катушки; остальное: не разобрано."""
    if isinstance(node, list):
        return [_flat(x) for x in node]
    if not isinstance(node, dict):
        return node
    c = node.get("call")
    if isinstance(c, dict):
        body = c.get("body")
        if isinstance(body, list):
            coils = [e for e in body if e.get("k") == "coil"]
            if len(coils) == 1:
                return _flat(coils[0].get("c"))
        return {"unparsed": f"call {c.get('blk')}"}
    return {k: (_flat(v) if k in ("and", "or", "not", "cmp", "a", "b") else v) for k, v in node.items()}


def _const(k: Any) -> Any:
    s = str(k).strip()
    if s.upper() == "TRUE":
        return True
    if s.upper() == "FALSE":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return MISSING


def _is_var(n: Any) -> bool:
    return (isinstance(n, dict) and isinstance(n.get("v"), str)
            and not any(k in n for k in ("edge", "range", "sr", "cmp", "and", "or", "not", "timer", "fn", "ctr", "xor")))


def _get(node: Any, vals: dict[str, Any]) -> Any:
    """Значение операнда (сигнал или константа) или MISSING."""
    if not isinstance(node, dict):
        return MISSING
    if "k" in node:
        return _const(node["k"])
    if _is_var(node) and not node.get("l"):
        return vals.get(norm_name(node["v"]), MISSING)
    return MISSING


def ev(node: Any, vals: dict[str, Any]) -> bool | None:
    """Три состояния: True / False / None (не вычислить: нет значения или не разобрано)."""
    if node is None:
        return True                                   # «всегда» (шина питания)
    if not isinstance(node, dict):
        return None
    if "and" in node:
        r = [ev(x, vals) for x in node["and"]]
        return False if False in r else (None if None in r else True)
    if "or" in node:
        r = [ev(x, vals) for x in node["or"]]
        return True if True in r else (None if None in r else False)
    if "not" in node:
        r1 = ev(node["not"], vals)
        return None if r1 is None else not r1
    if "cmp" in node:
        a, b = _get(node.get("a"), vals), _get(node.get("b"), vals)
        if a is MISSING or b is MISSING:
            return None
        try:
            return {"==": a == b, "<>": a != b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[node["cmp"]]
        except (KeyError, TypeError):
            return None
    if "range" in node:
        lo, v, hi = (_get(node.get(k), vals) for k in ("min", "v", "max"))
        if MISSING in (lo, v, hi):
            return None
        inside = lo <= v <= hi
        return inside if node["range"] == "in" else not inside
    if "timer" in node:
        inst = node.get("inst")
        if not inst:
            return None
        q = vals.get(norm_name(inst + ".Q"), MISSING)
        return None if q is MISSING else bool(q)
    if "sr" in node:
        return ev(node.get("v"), vals)
    if "k" in node:
        c = _const(node["k"])
        return c if isinstance(c, bool) else None
    if _is_var(node):
        if node.get("l"):
            return None
        v = vals.get(norm_name(node["v"]), MISSING)
        if v is MISSING:
            return None
        return (not bool(v)) if node.get("n") else bool(v)
    return None


def leaf_vars(node: Any) -> list[str]:
    """Имена сигналов, значения которых нужны для вычисления, в порядке появления в условии."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if "timer" in n:
                if n.get("inst"):
                    out.append(n["inst"] + ".Q")
                return
            if _is_var(n):
                if not n.get("l"):
                    out.append(n["v"])
                return
            for key in ("and", "or", "not", "a", "b", "min", "max", "v"):
                if key in n and isinstance(n[key], (dict, list)):
                    walk(n[key])

    walk(node)
    return list(dict.fromkeys(out))


def count_unparsed(node: Any) -> int:
    if isinstance(node, list):
        return sum(count_unparsed(x) for x in node)
    if not isinstance(node, dict):
        return 0
    n = 1 if "unparsed" in node else 0
    return n + sum(count_unparsed(v) for v in node.values() if isinstance(v, (dict, list)))


def blockers(node: Any, vals: dict[str, Any]) -> list[dict[str, Any]]:
    """Минимальный набор причин, почему условие ложно: для И все ложные дочерние, для ИЛИ самая «близкая» ветка
    (меньше всего ложных сигналов). need: каким сигнал должен быть (True включён, False выключен), None для сравнений."""
    if not isinstance(node, dict):
        return []
    if "and" in node:
        res: list[dict[str, Any]] = []
        for x in node["and"]:
            if ev(x, vals) is False:
                res.extend(blockers(x, vals))
        return res
    if "or" in node:
        cands = [blockers(x, vals) for x in node["or"] if ev(x, vals) is False]
        return min(cands, key=len) if cands else []
    if "not" in node:
        return [{"tag": None, "need": None, "kind": "complex", "text": to_text(node)}]
    if "cmp" in node:
        tag = next((x["v"] for x in (node.get("a"), node.get("b")) if _is_var(x) and not x.get("l")), None)
        return [{"tag": tag, "need": None, "kind": "cmp", "text": to_text(node)}]
    if "range" in node:
        tag = node["v"]["v"] if _is_var(node.get("v")) else None
        return [{"tag": tag, "need": None, "kind": "cmp", "text": to_text(node)}]
    if "timer" in node:
        return [{"tag": (node.get("inst") or "") + ".Q" if node.get("inst") else None, "need": True, "kind": "timer", "text": to_text(node)}]
    if "sr" in node:
        return blockers(node.get("v"), vals)
    if _is_var(node):
        return [{"tag": node["v"], "need": not node.get("n"), "kind": "var", "text": to_text(node)}]
    return [{"tag": None, "need": None, "kind": "complex", "text": to_text(node)}]


def route(node: Any, vals: dict[str, Any]) -> list[bool | None]:
    """Состояния отдельных условий на «ближайшем пути» включения (для счёта «выполнено 6 из 7»): И даёт все дочерние,
    ИЛИ только одну ветку (истинную, а если таких нет, самую близкую)."""
    if isinstance(node, dict) and "and" in node:
        return [x for c in node["and"] for x in route(c, vals)]
    if isinstance(node, dict) and "or" in node:
        true_b = [c for c in node["or"] if ev(c, vals) is True]
        if true_b:
            return route(true_b[0], vals)
        false_b = [c for c in node["or"] if ev(c, vals) is False]
        if false_b:
            return route(min(false_b, key=lambda c: len(blockers(c, vals))), vals)
        return [None]
    return [ev(node, vals)]


def supports(node: Any, vals: dict[str, Any]) -> list[dict[str, Any]]:
    """Что делает условие истинным сейчас (для аварий: что её держит): сигналы и их текущее состояние."""
    if not isinstance(node, dict):
        return []
    if "and" in node:
        res: list[dict[str, Any]] = []
        for x in node["and"]:
            res.extend(supports(x, vals))
        return res
    if "or" in node:
        cands = [supports(x, vals) for x in node["or"] if ev(x, vals) is True]
        return min(cands, key=len) if cands else []
    if _is_var(node) and not node.get("l"):
        return [{"tag": node["v"], "state": not node.get("n"), "kind": "var"}]
    if "cmp" in node or "range" in node:
        tag = next((x["v"] for x in (node.get("a"), node.get("b"), node.get("v")) if _is_var(x) and not x.get("l")), None)
        return [{"tag": tag, "state": None, "kind": "cmp"}]
    if "timer" in node and node.get("inst"):
        return [{"tag": node["inst"] + ".Q", "state": True, "kind": "timer"}]
    return []


# ------------------------------------------------------------------ тип А: почему выход не включён
def _find_chains(logic: tia_logic.TiaLogic, output: str) -> tuple[list[dict[str, Any]], str | None]:
    """Записи выхода (катушки включения). Имя без префикса ищется по окончанию, но только если оно однозначно."""
    chains = logic.chains_for(output, CALL_DEPTH)
    if not chains:
        chains = logic.chains_for(output, CALL_DEPTH, partial=True)
        if len({norm_name(c["o"]) for c in chains}) > 1:
            return [], "имя выхода неоднозначно: укажите полное"
    return [c for c in chains if c.get("k") in ("coil", "set")], None


def _decorate(items: list[dict[str, Any]], names: dict[str, tuple[str, str]], since: dict[str, int]) -> list[dict[str, Any]]:
    out = []
    for i in items:
        nm, place, named = friendly(names, i.get("tag"))
        out.append({"tech_name": i.get("tag"), "simple_name": nm, "named": named, "place": place,
                    "since_sec": since.get(norm_name(i["tag"])) if i.get("tag") else None, "need": i.get("need"),
                    "kind": i.get("kind")})
    return out


def analyze_output(logic: tia_logic.TiaLogic, output: str, vals: dict[str, Any], names: dict[str, tuple[str, str]],
                   since: dict[str, int], read_error: str | None = None) -> dict[str, Any]:
    """Чистое ядро типа А: логика + значения -> ответ (без БД и сети)."""
    res: dict[str, Any] = {"state": "unknown", "output": output, "missing": [], "satisfied": 0, "total": 0, "unparsed": 0,
                           "unread": [], "output_on": None, "note": None}
    chains, err = _find_chains(logic, output)
    if err or not chains:
        res["note"] = err or "логика этого выхода не найдена"
        return res
    out_name = chains[0]["o"]
    res["output"] = out_name
    res["output_simple"] = friendly(names, out_name)[0] if names.get(norm_name(out_name)) else None
    if read_error:
        res["note"] = read_error
    on = vals.get(norm_name(out_name), MISSING)
    res["output_on"] = None if on is MISSING else bool(on)
    conds = [_flat(c.get("c")) for c in chains]
    res["unparsed"] = sum(count_unparsed(c) for c in conds)
    res["unread"] = sorted({v for c in conds for v in leaf_vars(c) if norm_name(v) not in vals})
    if res["output_on"]:
        res["state"] = "ok"
        return res
    evs = [ev(c, vals) for c in conds]
    if True in evs:
        res["note"] = "условия выполнены, но выход выключен"
        res["unparsed"] = max(res["unparsed"], 0)
        return res
    if False not in evs:
        res["note"] = res["note"] or "часть логики не разобрана или значения не прочитаны"
        return res
    best = None
    for c, e in zip(conds, evs):
        if e is False:
            b = blockers(c, vals)
            if best is None or len(b) < len(best[1]):
                best = (c, b)
    cond, bl = best
    path = route(cond, vals)
    res["total"] = len(path)
    res["satisfied"] = sum(1 for x in path if x is True)
    res["missing"] = _decorate(bl, names, since)
    res["state"] = "blocked"
    return res


async def explain_output(plc_id: int, output_name: str, *, pool: asyncpg.Pool | None = None,
                         logic: tia_logic.TiaLogic | None = None, values: dict[str, Any] | None = None,
                         since: dict[str, int] | None = None, names: dict[str, tuple[str, str]] | None = None,
                         reader: Callable[[int, list[str]], Awaitable[dict[str, Any]]] | None = None,
                         now: datetime | None = None, timeout: float = READ_TIMEOUT) -> dict[str, Any]:
    """Тип А. {state ok|blocked|unknown, missing:[{tech_name, simple_name, place, since_sec}], satisfied, total, unparsed, checked_at}."""
    now = now or datetime.now(timezone.utc)
    if logic is None and pool is not None:
        logic = await logic_for_plc(pool, plc_id)
    if logic is None:
        return {"state": "unknown", "output": output_name, "missing": [], "satisfied": 0, "total": 0, "unparsed": 0,
                "unread": [], "output_on": None, "note": "логика ПЛК не загружена", "checked_at": now.isoformat()}
    chains, _ = _find_chains(logic, output_name)
    leaves: list[str] = []
    for c in chains:
        leaves += leaf_vars(_flat(c.get("c")))
    if chains:
        leaves.append(chains[0]["o"])
    leaves = list(dict.fromkeys(leaves))
    err = None
    tag_ids: dict[str, int] = {}
    if values is None:
        vals, tag_ids, err = await read_live(pool, plc_id, leaves, reader=reader, timeout=timeout) if leaves else ({}, {}, None)
    else:
        vals = {norm_name(k): v for k, v in values.items()}
    names = await load_names(pool, plc_id) if names is None and pool is not None else {norm_name(k): v for k, v in (names or {}).items()}
    if since is None:
        since = await load_since(pool, tag_ids, vals, now) if pool is not None and vals else {}
    else:
        since = {norm_name(k): v for k, v in since.items()}
    res = analyze_output(logic, output_name, vals, names, since, err)
    res["checked_at"] = now.isoformat()
    return res


# ------------------------------------------------------------------ тип Б: что за авария
def alarm_calls(logic: tia_logic.TiaLogic) -> dict[int, dict[str, Any]]:
    """bit_index -> вызов блока сборки аварии, найденный по адресу MSG.W[слово].x<бит> в заголовке сети (счётчик адреса в самой логике
    считается в SCL при работе ПЛК и из выгрузки не вычисляется, поэтому берётся комментарий сети)."""
    cached = getattr(logic, "_why_alarm_calls", None)
    if cached is not None:
        return cached
    res: dict[int, dict[str, Any]] = {}
    for bname, b in logic.blocks.items():
        for net in b.get("nets", []):
            m = ALARM_ADDR.search(net.get("t") or "")
            if not m:
                continue
            for uid, c in net.get("calls", {}).items():
                if c.get("blk") in MSG_BLOCKS:
                    res.setdefault(int(m[1]) * 16 + int(m[2]), {"block": bname, "net": net["n"], "title": net.get("t", ""), "call": c})
    logic._why_alarm_calls = res  # type: ignore[attr-defined]
    return res


def alarm_condition(call: dict[str, Any]) -> Any:
    """Условие появления аварии: источник И предусловие (и разрешение вызова, если есть)."""
    a = call.get("args", {})
    parts = [a.get("MsgSource"), a.get("MsgPrecondition"), call.get("en")]
    return tia_logic._and(*[p for p in parts if p is not None]) if any(p is not None for p in parts) else None


def history_stats(events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """За 7 дней: сколько раз была и сколько обычно длилась (медиана по снятым, с известным началом)."""
    since = now - timedelta(days=HISTORY_DAYS)
    recent = [e for e in events if e["started_at"] >= since]
    durs = [(e["ended_at"] - e["started_at"]).total_seconds() for e in recent if e.get("ended_at") and not e.get("active_at_start")]
    ended = [e["ended_at"] for e in events if e.get("ended_at")]
    return {"count_7d": len(recent), "typical_sec": int(statistics.median(durs)) if durs else None,
            "last_ended_at": max(ended).isoformat() if ended else None}


def analyze_alarm(logic: tia_logic.TiaLogic | None, bit_index: int, vals: dict[str, Any], names: dict[str, tuple[str, str]],
                  since: dict[str, int], catalog: dict[str, Any] | None, help_row: dict[str, str] | None,
                  events: list[dict[str, Any]], now: datetime, read_error: str | None = None) -> dict[str, Any]:
    """Чистое ядро типа Б."""
    open_ev = next((e for e in events if not e.get("ended_at")), None)
    res: dict[str, Any] = {
        "state": "unknown", "alarm_text": (catalog or {}).get("text") or "", "station": (catalog or {}).get("station") or "",
        "active_now": open_ev is not None, "active_since": open_ev["started_at"].isoformat() if open_ev else None,
        "active_for_sec": int((now - open_ev["started_at"]).total_seconds()) if open_ev else None,
        "source": None, "source_chain": None, "source_state": "unknown", "active_signals": [], "help": help_row,
        "history": history_stats(events, now), "unparsed": 0, "unread": [], "note": None}
    call = alarm_calls(logic).get(bit_index) if logic is not None else None
    if call is None:
        res["note"] = "источник аварии в логике не найден"
        return res
    cond = _flat(alarm_condition(call["call"]))
    res["source"] = {"block": call["block"], "net": call["net"], "title": call["title"]}
    res["source_chain"] = to_text(cond)
    res["unparsed"] = count_unparsed(cond)
    res["unread"] = sorted(v for v in leaf_vars(cond) if norm_name(v) not in vals)
    res["state"] = "found"
    if read_error:
        res["note"] = read_error
    r = ev(cond, vals)
    res["source_state"] = {True: "holding", False: "gone", None: "unknown"}[r]
    if r is True:
        sup = [s for s in supports(cond, vals) if not (s.get("tag") or "").lower().startswith(SKIP_SIGNALS)]
        res["active_signals"] = [{"tech_name": s["tag"], "simple_name": friendly(names, s["tag"])[0], "named": friendly(names, s["tag"])[2],
                                  "place": friendly(names, s["tag"])[1], "state": s["state"],
                                  "since_sec": since.get(norm_name(s["tag"])) if s.get("tag") else None} for s in sup]
    return res


async def _alarm_rows(pool: asyncpg.Pool, plc_id: int, bit: int, now: datetime) -> tuple[Any, Any, list[dict[str, Any]]]:
    cat = await pool.fetchrow("SELECT text, station FROM alarm_catalog WHERE plc_id = $1 AND bit_index = $2", plc_id, bit)
    help_row = None
    try:
        r = await pool.fetchrow(
            "SELECT simple_description, typical_causes, fix_steps FROM alarm_help WHERE plc_id = $1 AND bit_index = $2", plc_id, bit)
        if r and (r["simple_description"] or r["typical_causes"] or r["fix_steps"]):
            help_row = {"simple": r["simple_description"], "causes": r["typical_causes"], "fix": r["fix_steps"]}
    except asyncpg.PostgresError:
        help_row = None
    rows = await pool.fetch(
        "SELECT started_at, ended_at, active_at_start FROM alarm_events WHERE plc_id = $1 AND bit_index = $2 "
        "AND (started_at >= $3 OR ended_at IS NULL) ORDER BY started_at", plc_id, bit, now - timedelta(days=HISTORY_DAYS))
    return (dict(cat) if cat else None), help_row, [dict(r) for r in rows]


async def explain_alarm(plc_id: int, bit_index: int, *, pool: asyncpg.Pool | None = None, logic: tia_logic.TiaLogic | None = None,
                        values: dict[str, Any] | None = None, since: dict[str, int] | None = None,
                        names: dict[str, tuple[str, str]] | None = None, catalog: dict[str, Any] | None = None,
                        help: dict[str, str] | None = None, events: list[dict[str, Any]] | None = None,  # noqa: A002
                        reader: Callable[[int, list[str]], Awaitable[dict[str, Any]]] | None = None,
                        now: datetime | None = None, timeout: float = READ_TIMEOUT) -> dict[str, Any]:
    """Тип Б: {alarm_text, active_now, active_since, source_chain, active_signals, help, history, unparsed, state}."""
    now = now or datetime.now(timezone.utc)
    if logic is None and pool is not None:
        logic = await logic_for_plc(pool, plc_id)
    if pool is not None and (catalog is None or events is None or help is None):
        c, h, e = await _alarm_rows(pool, plc_id, bit_index, now)
        catalog = c if catalog is None else catalog
        help = h if help is None else help  # noqa: A001
        events = e if events is None else events
    events = events or []
    call = alarm_calls(logic).get(bit_index) if logic is not None else None
    cond = _flat(alarm_condition(call["call"])) if call else None
    leaves = leaf_vars(cond) if call else []
    err = None
    tag_ids: dict[str, int] = {}
    if values is None:
        vals, tag_ids, err = await read_live(pool, plc_id, leaves, reader=reader, timeout=timeout) if leaves else ({}, {}, None)
    else:
        vals = {norm_name(k): v for k, v in values.items()}
    names = await load_names(pool, plc_id) if names is None and pool is not None else {norm_name(k): v for k, v in (names or {}).items()}
    if since is None:
        since = await load_since(pool, tag_ids, vals, now) if pool is not None and vals else {}
    else:
        since = {norm_name(k): v for k, v in since.items()}
    res = analyze_alarm(logic, bit_index, vals, names, since, catalog, help, events, now, err)
    res["checked_at"] = now.isoformat()
    return res


# ------------------------------------------------------------------ тексты для киповца
def fmt_dur(sec: int | float | None) -> str:
    """«45 с», «4 мин 20 с», «12 мин», «2 ч 5 мин»."""
    if sec is None:
        return ""
    s = int(sec)
    if s < 60:
        return f"{s} с"
    m, r = divmod(s, 60)
    if m < 60:
        return f"{m} мин {r} с" if r else f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def _first_places(items: list[dict[str, Any]]) -> str:
    return "; ".join(dict.fromkeys(i["place"] for i in items if i.get("place")))


def _names_line(items: list[dict[str, Any]]) -> str:
    shown = items[:MAX_SHOWN]
    s = ", ".join(i["simple_name"] for i in shown)
    if len(items) > MAX_SHOWN:
        s += f" и ещё {len(items) - MAX_SHOWN}"
    return s


def _limit(lines: list[str]) -> str:
    return "\n".join([l for l in lines if l.strip()][:MAX_LINES])


def render_simple_output(res: dict[str, Any]) -> str:
    """Ответ киповцу по типу А: что не так, где смотреть, сколько уже, что выполнено; не больше 6 строк, без имён тегов (если есть простые)."""
    nm = res.get("output_simple")
    state = res.get("state")
    if state == "ok":
        return _limit([f"Всё в порядке: «{nm}» включено." if nm else "Всё в порядке: выход уже включён."])
    if state == "blocked":
        miss = res.get("missing", [])
        need_on = [m for m in miss if m.get("need") is not False]
        need_off = [m for m in miss if m.get("need") is False]
        head = f"Не включается: «{nm}»." if nm else "Оборудование ждёт условие и не включается."
        lines = [head]
        if need_on:
            lines.append(f"Не хватает: {_names_line(need_on)}.")
        if need_off:
            lines.append(f"Мешает (должно быть выключено): {_names_line(need_off)}.")
        shown = (need_on + need_off)[:MAX_SHOWN]
        place = _first_places(shown)
        if place:
            lines.append(f"Где смотреть: {place}.")
        first = shown[0] if shown else None
        if first is not None:
            lines.append(f"Уже {fmt_dur(first['since_sec'])}." if first.get("since_sec") is not None else "Сколько времени так, неизвестно.")
        tail = ""
        if res.get("total"):
            tail = f"Остальные условия выполнены ({res['satisfied']} из {res['total']})."
        if res.get("unparsed") or res.get("unread"):
            tail = (tail[:-1] + "; " if tail else "") + "часть логики не разобрана."
        if any(not m.get("named", True) for m in shown):
            tail = (tail + " " if tail else "") + "Для инженера: нет простого названия сигнала."
        lines.append(tail)
        return _limit(lines)
    note = res.get("note") or "часть логики не разобрана"
    lines = ["Точную причину назвать не могу.", note[0].upper() + note[1:] + "."]
    if res.get("unparsed") or res.get("unread"):
        lines.append("Часть логики не разобрана.")
    return _limit(lines)


def _steps(help_row: dict[str, str] | None) -> str:
    if not help_row or not (help_row.get("fix") or "").strip():
        return "инструкции пока нет"
    steps = [s.strip(" -•\t") for s in help_row["fix"].splitlines() if s.strip()]
    text = "; ".join(steps[:3])
    return text[:240] + ("…" if len(text) > 240 else "")


def render_simple_alarm(res: dict[str, Any]) -> str:
    """Ответ киповцу по типу Б: что случилось, что держит, где смотреть, что делать, как часто бывает; не больше 6 строк."""
    h = res.get("help") or {}
    what = h.get("simple") or res.get("alarm_text") or "авария без описания"
    st = res.get("station")
    lines = [f"Авария{' (' + st + ')' if st else ''}: {what.rstrip('.')}."]
    hist = res.get("history", {})
    if not res.get("active_now"):
        last = hist.get("last_ended_at")
        lines.append("Сейчас авария не активна." if not last else "Сейчас авария не активна, ранее уже снималась.")
    elif res.get("state") != "found":
        lines.append("Что её держит, по логике определить не удалось.")
    elif res.get("source_state") == "gone":
        lines.append("Причина уже пропала, авария ждёт сброса.")
    elif res.get("active_signals"):
        sig = res["active_signals"]
        parts = [f"{s['simple_name']} ({'сигнал есть' if s['state'] else 'сигнала нет'})" if s.get("state") is not None else s["simple_name"]
                 for s in sig[:MAX_SHOWN]]
        t = ", ".join(parts) + (f" и ещё {len(sig) - MAX_SHOWN}" if len(sig) > MAX_SHOWN else "")
        dur = sig[0].get("since_sec") if sig[0].get("since_sec") is not None else res.get("active_for_sec")
        lines.append(f"Сейчас держит: {t}" + (f", уже {fmt_dur(dur)}." if dur is not None else "."))
        place = _first_places(sig[:MAX_SHOWN])
        if place:
            lines.append(f"Где смотреть: {place}.")
    else:
        lines.append("Что её держит, по значениям определить не удалось.")
    lines.append(f"Что сделать: {_steps(res.get('help'))}.")
    c = hist.get("count_7d", 0)
    if c:
        typ = hist.get("typical_sec")
        lines.append(f"За {HISTORY_DAYS} дней было {c} раз" + (f", обычно снимается за {fmt_dur(typ)}." if typ is not None else "."))
    else:
        lines.append(f"За {HISTORY_DAYS} дней такого не было.")
    if res.get("unparsed") or res.get("unread"):
        lines.append("Часть логики не разобрана.")
    return _limit(lines)


# ------------------------------------------------------------------ API (только чтение)
def _timeout_result(kind: str) -> dict[str, Any]:
    return {"state": "unknown", "note": "ответ не получен за %d с" % READ_TIMEOUT, "missing": [], "active_signals": [],
            "unparsed": 0, "unread": [], "kind": kind}


@router.get("/output")
async def why_output(request: Request, plc_id: int = Query(ge=1), output: str = Query(min_length=1, max_length=200),
                     _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        res = await asyncio.wait_for(explain_output(plc_id, output, pool=request.app.state.pool), READ_TIMEOUT + 1)
    except asyncio.TimeoutError:
        res = _timeout_result("output")
    return {"result": res, "text": render_simple_output(res)}


@router.get("/alarm")
async def why_alarm(request: Request, plc_id: int = Query(ge=1), bit: int = Query(ge=0, le=100000),
                    _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        res = await asyncio.wait_for(explain_alarm(plc_id, bit, pool=request.app.state.pool), READ_TIMEOUT + 1)
    except asyncio.TimeoutError:
        res = {**_timeout_result("alarm"), "history": {}, "active_now": False}
    return {"result": res, "text": render_simple_alarm(res)}
