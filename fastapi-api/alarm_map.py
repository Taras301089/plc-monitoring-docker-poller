"""Карта аварий ПЛК: номер аварийного бита HMI -> какая сеть какого блока и по какому условию его ставит (S-14).

Зачем: киповцу нужен ответ «авария X: что это и что её держит сейчас». Номера бит в ПЛК присваиваются во время работы счётчиками
G.Err_Word / G.Err_Bit (каждый вызов 02Msg_Stn / 01Msg_Cell занимает следующий бит, 16 бит = слово MSG.W[n]; 03Msg_Word и 01Stn_Total
задают слово напрямую), поэтому в коде номера не прописаны. Здесь порядок вызовов моделируется: от OB1 идёт обход вызовов блоков
(FC, FB с экземплярами, мультиэкземпляры) в порядке сетей, счётчик слова и бита и номер станции G.stn_No ведутся как в ПЛК, на каждый вызов
сообщения записывается «слово, бит, цепочка вызовов, тип, станция, MsgSource, MsgPrecondition».

Чего здесь нет: ничего не угадывается. Если порядок нельзя вычислить (изменение счётчика в цикле или по условию), бит помечается
флагом, а сверка с каталогом аварий (alarm_catalog) говорит, чем подтверждён результат: text (текст или подпись сети совпали),
order (только порядок), none (не сопоставлено). Только разбор готового JSON логики, без БД и без связи с ПЛК.
Запуск как скрипт:
    python alarm_map.py <ZIP или папка выгрузки или JSON логики> [-o alarms.json] [--bit N]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))    # запуск как скрипта: соседние модули находятся и без PYTHONPATH
import tia_logic as T  # noqa: E402

# Блоки сообщений: лист обхода (сообщение занимает один бит) -> область
MSG_LEAF = {"02Msg_Stn": "stn", "01Msg_Cell": "cell", "07Msg_Estop": "estop"}
MAX_DEPTH = 14
TEXT_CAP = 1600
BITS_TOTAL = 12000
ALIAS_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:=\s*([A-Za-z_\"][\w\.\[\]\"/ ]*?)\s*;")
G_ASSIGN_RE = re.compile(r"^\s*G\.(\w+)\s*:=\s*(.+?)\s*;")
SCL_OPEN_RE = re.compile(r"\b(IF|CASE|FOR|WHILE|REPEAT)\b")
SCL_CLOSE_RE = re.compile(r"\bEND_(IF|CASE|FOR|WHILE|REPEAT)\b")
ADDR_IN_TITLE = re.compile(r"MSG\.W\[(\d+)\]\.x(\d+)", re.I)


def _unq(name: str) -> str:
    return ".".join(T.split_name(name))


def _cap(s: str, n: int = TEXT_CAP) -> str:
    return s if len(s) <= n else s[: n - 2] + " …"


def fold(node: Any) -> Any:
    """Сравнения чисел (после подстановки параметров) заменяются на TRUE/FALSE и убираются из И/ИЛИ: (2 >= 1) И X -> X."""
    if isinstance(node, list):
        return [fold(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = {k: (fold(v) if k not in ("_x", "v") else v) for k, v in node.items()}
    if "cmp" in node:
        a, b = node.get("a"), node.get("b")
        if isinstance(a, dict) and isinstance(b, dict) and "k" in a and "k" in b:
            try:
                x, y = int(str(a["k"]).split("#")[-1]), int(str(b["k"]).split("#")[-1])
                ops = {"==": x == y, "<>": x != y, ">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}
                return {"k": "TRUE" if ops[node["cmp"]] else "FALSE"}
            except (ValueError, KeyError):
                return node
    if "and" in node:
        items = [x for x in node["and"] if not (isinstance(x, dict) and x.get("k") == "TRUE")]
        if any(isinstance(x, dict) and x.get("k") == "FALSE" for x in items):
            return {"k": "FALSE"}
        return {"k": "TRUE"} if not items else (items[0] if len(items) == 1 else {"and": items})
    if "or" in node:
        items = [x for x in node["or"] if not (isinstance(x, dict) and x.get("k") == "FALSE")]
        if any(isinstance(x, dict) and x.get("k") == "TRUE" for x in items):
            return {"k": "TRUE"}
        return {"k": "FALSE"} if not items else (items[0] if len(items) == 1 else {"or": items})
    return node


def _plain(node: Any) -> Any:
    """Дерево условия без служебных ключей (_x) для текста и JSON."""
    if isinstance(node, list):
        return [_plain(x) for x in node]
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items() if k != "_x"}
    return node


def txt(node: Any) -> str:
    return _cap(T.to_text(_plain(fold(node))))


class Frame:
    """Один вызов блока на пути от OB1: блок, путь экземпляра (Static-данные), фактические аргументы (уже в терминах OB1)."""

    def __init__(self, block: dict[str, Any], path: list[str], env: dict[str, Any], via: str, parent: "Frame | None") -> None:
        self.block = block
        self.path = path
        self.env = env
        self.via = via              # как вызван: "блок:сеть [экземпляр]"
        self.parent = parent
        self.sec: dict[str, str] = {}
        for sec, mem in block["if"].items():
            for m in mem:
                self.sec.setdefault(T.norm_name(m[0]), sec)
        self.cond = False           # вызов внутри условия/цикла или с входом EN
        self.always: set[str] = set()   # локальные «всегда истина»: X <= НЕ X ИЛИ X (приём ALWAYS_ON)
        for net in block["nets"]:
            for w in net.get("w", []):
                c = w.get("c")
                if w.get("l") and w["k"] == "coil" and isinstance(c, dict) and len(c.get("or", [])) == 2:
                    a, b = c["or"]
                    if isinstance(a, dict) and isinstance(b, dict) and a.get("v") == b.get("v") == w["o"] and bool(a.get("n")) != bool(b.get("n")):
                        self.always.add(T.norm_name(w["o"]))
        self.stn: int | None = None  # G.stn_No в момент входа в блок
        self.aliases: dict[str, str] = {}
        for net in block["nets"]:
            if "scl" in net:
                depth = 0
                for line in net["scl"].splitlines():
                    code = line.split("//")[0]
                    m = ALIAS_RE.match(code)
                    if m and depth == 0 and self.sec.get(T.norm_name(m.group(1))) == "Temp" and not m.group(1).startswith("G."):
                        self.aliases.setdefault(T.norm_name(m.group(1)), m.group(2).strip())
                    depth += len(SCL_OPEN_RE.findall(code)) - len(SCL_CLOSE_RE.findall(code))
        self.net = 0                # сеть вызывающего блока, из которой вызван этот блок
        self.inst = ""              # экземпляр (если FB)
        self.depth = 0 if parent is None else parent.depth + 1

    def chain(self) -> list["Frame"]:
        res, f = [], self
        while f is not None:
            res.append(f)
            f = f.parent
        return res[::-1]


class AlarmMap:
    def __init__(self, logic: T.TiaLogic) -> None:
        self.logic = logic
        self.recs: list[dict[str, Any]] = []
        self.g: dict[str, Any] = {"err_word": 0, "err_bit": 0, "stn_no": None}
        self.tainted: str | None = None      # причина, по которой текущий номер бита ненадёжен
        self.warnings: list[str] = []
        self.missing: dict[str, int] = {}
        self.stn_names: dict[int, str] = {}

    # ---------- значения ----------
    def subst(self, node: Any, fr: Frame) -> Any:
        """Условие из блока fr в термины OB1: параметры -> фактические аргументы, Static-данные -> полный путь экземпляра."""
        if isinstance(node, list):
            return [self.subst(x, fr) for x in node]
        if not isinstance(node, dict):
            return node
        if "v" in node and node.get("l") and isinstance(node["v"], str):
            parts = T.split_name(node["v"])
            root = T._root(node["v"])
            sec = fr.sec.get(T.norm_name(root))
            rest = ".".join(T._q(p) for p in parts[1:])
            arg = fr.env.get(root)
            if arg is None:
                for k, a in fr.env.items():
                    if T.norm_name(k) == T.norm_name(root):
                        arg = a
                        break
            if sec in ("Input", "InOut") and arg is not None:
                res = dict(arg) if isinstance(arg, dict) else arg
                if rest:
                    if isinstance(res, dict) and "v" in res:
                        res["v"] = res["v"] + "." + rest
                    else:
                        return {"v": f"<{root}>.{rest}", "l": 1}
                if node.get("n"):
                    res = T._not(res)
                return res
            if sec == "Temp" and not rest and T.norm_name(root) in fr.always:
                return {"k": "FALSE" if node.get("n") else "TRUE"}
            al = fr.aliases.get(T.norm_name(root)) if sec == "Temp" and not rest else None
            if al:
                res = {"v": re.sub(r"G\.stn_No", str(fr.stn) if fr.stn is not None else "G.stn_No", al)}
                if node.get("n"):
                    res["n"] = 1
                return res
            if sec in ("Static", "Output", "InOut", "Return", "Input") and fr.path:
                res = {"v": ".".join(fr.path + [T._q(p) for p in parts]), "_x": (fr, ".".join(T._q(p) for p in parts))}
                if node.get("n"):
                    res["n"] = 1
                return res
            return node
        return {k: (self.subst(v, fr) if k != "o" else v) for k, v in node.items()}

    def to_int(self, node: Any, fr: Frame | None) -> int | None:
        """Целое значение узла: fr — блок, в терминах которого записан узел (None: уже в терминах OB1); константа, G.xxx, Add."""
        if fr is not None:
            node = self.subst(node, fr)
        if not isinstance(node, dict):
            return None
        if "k" in node:
            m = re.fullmatch(r"(?:\w+#)?(-?\d+)", str(node["k"]).strip())
            return int(m.group(1)) if m else None
        if "v" in node and isinstance(node["v"], str) and not node.get("l"):
            m = re.fullmatch(r"G\.(\w+)", node["v"], re.I)
            if m:
                return self.g.get(m.group(1).lower())
        if node.get("fn") == "Add":
            vals = [self.to_int(x, None) for x in node["a"]]
            return None if any(v is None for v in vals) else sum(vals)
        return None

    def env_int(self, expr: str, fr: Frame) -> int | None:
        """Целое по тексту SCL: число, параметр блока, G.xxx, X + число."""
        expr = expr.strip().strip("()")
        m = re.fullmatch(r"(-?\d+)", expr)
        if m:
            return int(m.group(1))
        m = re.fullmatch(r"G\.(\w+)\s*([+-])\s*(\d+)", expr)
        if m:
            base = self.g.get(m.group(1).lower())
            return None if base is None else base + (int(m.group(3)) if m.group(2) == "+" else -int(m.group(3)))
        m = re.fullmatch(r"G\.(\w+)", expr)
        if m:
            return self.g.get(m.group(1).lower())
        if re.fullmatch(r"\w+", expr):
            return self.to_int({"v": expr, "l": 1}, fr)
        return None

    # ---------- эффекты блока на счётчики ----------
    def _set_g(self, name: str, val: int | None, where: str) -> None:
        key = name.lower()
        if key not in ("err_word", "err_bit", "stn_no", "cell_no"):
            return
        if val is None:
            if key in ("err_word", "err_bit"):
                self.tainted = f"{where}: значение G.{name} не вычислено"
            self.g[key] = None
            return
        self.g[key] = val

    def apply_net_effects(self, fr: Frame, net: dict[str, Any]) -> None:
        where = f"{fr.block['name']}:{net['n']}"
        scl = net.get("scl")
        if scl:
            depth = 0
            for line in scl.splitlines():
                code = line.split("//")[0]
                m = G_ASSIGN_RE.match(code)
                if m and m.group(1).lower() in ("err_word", "err_bit", "stn_no", "cell_no"):
                    if depth == 0:
                        self._set_g(m.group(1), self.env_int(m.group(2), fr), where)
                    else:
                        self.tainted = f"{where}: G.{m.group(1)} меняется внутри условия или цикла"
                depth += len(SCL_OPEN_RE.findall(code)) - len(SCL_CLOSE_RE.findall(code))
        for w in net.get("w", []):
            o = w["o"]
            if not re.fullmatch(r"G\.(Err_Word|Err_Bit|stn_No|Cell_No)", o, re.I):
                continue
            if w["k"] not in ("out", "move", "inc"):
                continue
            if w.get("c") is not None:
                self.tainted = f"{where}: {o} меняется по условию"
                continue
            if w["k"] == "inc":
                cur = self.g.get(o.split(".")[1].lower())
                self._set_g(o.split(".")[1], None if cur is None else cur + 1, where)
                continue
            self._set_g(o.split(".")[1], self.to_int(w.get("v"), fr), where)

    # ---------- обход ----------
    def run(self, root_name: str = "Main") -> None:
        root = self.logic.blocks.get(root_name)
        if root is None:
            raise ValueError(f"в выгрузке нет блока {root_name}")
        fr = Frame(root, [], {}, root_name, None)
        self.walk(fr, [root_name], set())

    def walk(self, fr: Frame, stack: list[str], seen: set[str]) -> None:
        if fr.depth > MAX_DEPTH:
            self.warnings.append(f"слишком глубокая вложенность в {fr.block['name']}")
            return
        for net in fr.block["nets"]:
            self.apply_net_effects(fr, net)
            fr.stn = self.g.get("stn_no")
            calls = net.get("calls", {})
            for uid, c in calls.items():
                blk = c.get("blk")
                if not blk:
                    continue
                cond = bool(c.get("cnd")) or c.get("en") is not None or fr.cond
                if blk in MSG_LEAF:
                    self.leaf(fr, net, uid, c, cond)
                    continue
                callee = self.logic.blocks.get(blk)
                if callee is None:
                    self.missing[blk] = self.missing.get(blk, 0) + 1
                    continue
                if blk in stack:
                    self.warnings.append(f"рекурсия {' > '.join(stack)} > {blk}")
                    continue
                nc = self._expand_calls(net, c)
                env = {k: self.subst(v, fr) for k, v in nc.items()}
                inst = c.get("inst")
                if c.get("type") == "FB" and inst:
                    path = (fr.path + [_unq(inst)]) if c.get("loc") else [_unq(inst)]
                else:
                    path = []
                via = f"{fr.block['name']}:{net['n']}" + (f" [{_unq(inst)}]" if inst else "")
                child = Frame(callee, path, env, via, fr)
                child.stn = self.g.get("stn_no")
                child.cond = cond
                child.net, child.inst = net["n"], (_unq(inst) if inst else "")
                if blk == "01Stn_Total":
                    self._note_station(env)
                self.walk(child, stack + [blk], seen)

    def _expand_calls(self, net: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
        calls = net.get("calls", {})
        return {k: self.logic._expand(v, calls, 0) for k, v in c.get("args", {}).items()}

    def leaf(self, fr: Frame, net: dict[str, Any], uid: str, c: dict[str, Any], cond: bool) -> None:
        loop = c.get("loop")
        if loop and loop.get("by", 1) > 0:
            for i in range(loop["from"], loop["to"] + 1, loop["by"]):
                self._leaf_once(fr, net, uid, c, cond, i)
        else:
            self._leaf_once(fr, net, uid, c, cond, None)

    def _leaf_once(self, fr: Frame, net: dict[str, Any], uid: str, c: dict[str, Any], cond: bool, loop_i: int | None) -> None:
        kind = MSG_LEAF[c["blk"]]
        args = self._expand_calls(net, c)
        word, bit = self.g.get("err_word"), self.g.get("err_bit")
        rec: dict[str, Any] = {"word": word, "bit": bit, "kind": kind, "msgblk": c["blk"], "frame": fr, "net": net["n"], "uid": uid,
                               "ntitle": net.get("t", ""), "args": args, "cond": cond, "taint": self.tainted, "loop_i": loop_i}
        mt = args.get("CFG_MsgType")
        if isinstance(mt, dict) and isinstance(mt.get("v"), str):
            rec["type"] = _unq(mt["v"]).split(".")[-1]
        elif isinstance(mt, dict) and "k" in mt:
            rec["type"] = str(mt["k"])
        if kind == "estop":
            rec["type"] = "Estop"
        sn = args.get("CFG_Stn_No" if kind == "stn" else "CFG_Cell_No")
        if sn is not None:
            rec["unit"] = self.to_int(sn, fr)
        self.recs.append(rec)
        # счётчик: +1 бит, на 16 новое слово
        if word is None or bit is None:
            self.tainted = self.tainted or f"{fr.block['name']}:{net['n']}: счётчик не определён"
            return
        bit += 1
        if bit == 16:
            word, bit = word + 1, 0
        self.g["err_word"], self.g["err_bit"] = word, bit

    def _note_station(self, env: dict[str, Any]) -> None:
        n = self.to_int(env.get("STN_NO"), None)
        t = env.get("txt")
        if n is not None and isinstance(t, dict) and "k" in t:
            m = re.search(r"'([^']*)'", str(t["k"]))
            if m:
                self.stn_names[n] = m.group(1)

    # ---------- объяснение источника ----------
    def writes_of(self, fr: Frame, local: str) -> list[dict[str, Any]]:
        """Записи переменной блока: сети, вид (set/reset/coil/...), условие в терминах OB1."""
        want = T.norm_name(local)
        res = []
        for net in fr.block["nets"]:
            for w in net.get("w", []):
                if w.get("l") and T.norm_name(w["o"]) == want:
                    cond = self.logic._expand(w.get("c"), net.get("calls", {}), 0)
                    res.append({"net": net["n"], "title": net.get("t", ""), "k": w["k"], "c": self.subst(cond, fr)})
        return res

    def explain(self, node: Any, depth: int = 2, seen: set | None = None) -> list[dict[str, Any]]:
        """Переменные-данные экземпляра в источнике и их записи (set/reset/coil); depth — сколько ступеней раскрывать."""
        seen = seen if seen is not None else set()
        out: list[dict[str, Any]] = []
        for leaf in _xleaves(node):
            fr, local = leaf["_x"]
            key = (id(fr), local)
            if key in seen or len(out) >= 14:
                continue
            seen.add(key)
            ws = self.writes_of(fr, local)
            if not ws:
                continue
            out.append({"var": leaf["v"], "block": fr.block["name"],
                        "writes": [{"net": w["net"], "t": w["title"], "k": w["k"], "c": txt(w["c"])} for w in ws]})
            if depth > 1:
                for w in ws:
                    out.extend(self.explain(w["c"], depth - 1, seen))
        return out

    # ---------- сверка с каталогом ----------
    def _ctx(self, rec: dict[str, Any], srctext: str = "") -> tuple[set[str], str]:
        fr: Frame = rec["frame"]
        texts = [rec["ntitle"]]
        names: list[str] = []
        unit = rec.get("unit")
        if rec["kind"] == "stn" and unit in self.stn_names:
            names.append(self.stn_names[unit])
        objname = ""
        for f in fr.chain():
            for k, v in f.env.items():
                if isinstance(v, dict) and isinstance(v.get("k"), str) and "'" in v["k"]:
                    m = re.search(r"'([^']*)'", v["k"])
                    if m:
                        texts.append(m.group(1))
                        if k == "OBJ_Name":
                            objname = m.group(1)
                if k in ("CFG_STN_NO", "STN_NO", "CFG_STN_No"):
                    n = self.to_int(v, None)
                    if n in self.stn_names:
                        names.append(self.stn_names[n])
                if k == "CFG_OP_NO":
                    n = self.to_int(v, None)
                    if n is not None:
                        texts.append(f"SOB{n}")
                if k == "OPB_NO":
                    n = self.to_int(v, None)
                    if n is not None:
                        texts.append(f"OPB{n}")
        src = rec["args"].get("MsgSource")
        m = re.search(r"(Fwd|Bwd)_0*(\d+)_fault", json.dumps(src) if src else "")
        if m and objname:
            texts.append(f"{objname.rsplit('_', 1)[-1]}.{m.group(2)}")
        return _tokens(" ".join(texts + names + [srctext])), " ".join(texts)

    def _loop_source(self, rec: dict[str, Any]) -> str:
        """Источник сообщения в цикле SCL: условие первого IF после заголовка FOR с подставленным номером шага."""
        net = next((n for n in rec["frame"].block["nets"] if n["n"] == rec["net"]), None)
        if not net or "scl" not in net:
            return ""
        call = net.get("calls", {}).get(rec["uid"], {})
        scl = net["scl"]
        pos = scl.find((call.get("txt") or "")[:40]) if call.get("txt") else -1
        start = scl.rfind("FOR", 0, pos if pos > 0 else len(scl))
        m = re.search(r"IF\s+(.+?)\s+THEN", scl[start:], re.S)
        if not m:
            return ""
        cond = re.sub(r"//[^\n]*", "", m.group(1))
        cond = re.sub(r"\s+", " ", cond).strip()
        var = (call.get("loop") or {}).get("var", "I")
        return "Err := " + re.sub(rf"\b{var}\b", str(rec["loop_i"]), cond)

    def build(self, catalog: dict[int, str] | None = None, built_from: str = "") -> dict[str, Any]:
        """Итоговая карта: {"alarms": {бит: запись}, "stats": ..., "meta": ...}; catalog {бит: текст аварии из БД} включает сверку."""
        catalog = catalog or {}
        alarms: dict[str, dict[str, Any]] = {}
        dups: dict[int, int] = {}
        for rec in self.recs:
            if rec["word"] is None or rec["bit"] is None:
                continue
            b = rec["word"] * 16 + rec["bit"]
            if b >= BITS_TOTAL:
                continue
            fr: Frame = rec["frame"]
            ent: dict[str, Any] = {"b": b, "w": rec["word"], "x": rec["bit"], "msg": rec["msgblk"], "blk": fr.block["name"], "net": rec["net"]}
            if rec["ntitle"]:
                ent["nt"] = rec["ntitle"]
            if rec.get("type"):
                ent["type"] = rec["type"]
            ent["kind"] = rec["kind"]
            unit = rec.get("unit")
            if unit is not None:
                ent["unit"] = unit
                if rec["kind"] == "stn" and unit in self.stn_names:
                    ent["unit_name"] = self.stn_names[unit]
            ent["path"] = _path(fr, rec)
            if rec["loop_i"] is not None:
                ent["loop_i"] = rec["loop_i"]
            src = rec["args"].get("MsgSource")
            pre = rec["args"].get("MsgPrecondition")
            flags: list[str] = []
            if rec["loop_i"] is not None:
                ent["src"] = self._loop_source(rec) or "(условие в цикле SCL)"
                ent["pre"] = "ВСЕГДА"
            else:
                sn = self.subst(src, fr) if src is not None else None
                ent["pre"] = txt(self.subst(pre, fr)) if pre is not None else "ВСЕГДА"
                chain = self.explain(sn) if sn is not None else []
                first = chain[0] if chain and _single_var(sn) else None
                if first:
                    sets = [w for w in first["writes"] if w["k"] in ("set", "coil", "out", "move", "sr", "rs", "callout")]
                    resets = [w for w in first["writes"] if w["k"] in ("reset", "rs", "sr")]
                    ent["src"] = _cap(f"{first['var']}: " + " ИЛИ ".join(f"[сеть {w['net']}] {w['c']}" for w in sets) if sets else first["var"])
                    ent["latched"] = bool(resets)
                    if resets:
                        ent["reset"] = _cap(" ИЛИ ".join(f"[сеть {w['net']}] {w['c']}" for w in resets))
                else:
                    ent["src"] = txt(sn) if sn is not None else "(MsgSource не задан)"
                    ent["latched"] = False
                if chain:
                    ent["chain"] = chain
            if rec["cond"]:
                flags.append("вызов в условии")
            if rec["taint"]:
                flags.append("номер бита ненадёжен: " + rec["taint"])
            ct = (catalog.get(b) or "").strip()
            tok, ctxs = self._ctx(rec, ent.get("src", ""))
            by = "order"
            if ct:
                ent["text"] = ct
                m = ADDR_IN_TITLE.search(rec["ntitle"] or "")
                if m and int(m.group(1)) == rec["word"] and int(m.group(2)) == rec["bit"]:
                    by = "text"
                    ent["by_addr"] = True
                elif rec["loop_i"] is not None:
                    by = "text" if re.search(rf"\b{rec['loop_i']}\b", ct) and "profinet" in ct.lower() else "order"
                else:
                    ctok = _tokens(ct)
                    common = ctok & tok
                    ident = any(any(ch.isdigit() for ch in t) for t in common)
                    if len(common) >= 2 and ident:
                        by = "text"
                    elif ident:
                        # слова каталога внутри имён сигналов и подписей (chain -> ChainBreak1): не меньше половины значимых слов
                        words = [w for w in ctok - common if len(w) >= 4 and not any(ch.isdigit() for ch in w)]
                        comp = _compact(ent.get("src", "") + " " + ctxs)
                        hits = [w for w in words if w in comp]
                        if hits and len(hits) * 2 >= len(words):
                            by = "text"
                if by != "text":
                    flags.append("текст аварии не совпал с подписью сети")
            elif catalog:
                flags.append("в каталоге аварий у этого бита нет текста")
            ent["by"] = by
            if flags:
                ent["flags"] = flags
            if b in alarms:
                dups[b] = dups.get(b, 1) + 1
                alarms[b].setdefault("flags", []).append("бит назначен несколько раз")
                continue
            alarms[b] = ent
        for b, ct in catalog.items():
            if ct.strip() and b not in alarms:
                alarms[b] = {"b": b, "w": b // 16, "x": b % 16, "by": "none", "text": ct.strip(), "flags": ["источник не найден"]}
        stats = self.stats(alarms, catalog)
        meta = {"built_from": built_from, "plc": self.logic.plc,
                "hmi_latch": ("Бит MSG.W[слово].x[бит] ставится, пока истинно Err = MsgSource И MsgPrecondition, и снимается кнопкой сброса ячейки "
                              "(Cell.CELL[n].zw.ErrRst), когда Err уже ложно; блоки 02Msg_Stn и 01Msg_Cell, сеть «Set reset alarm DB»."),
                "err_condition": self._err_condition(), "warnings": self.warnings[:20], "missing_blocks": self.missing}
        return {"alarms": {str(k): v for k, v in sorted(alarms.items())}, "stats": stats, "meta": meta}

    def _err_condition(self) -> str:
        for w in (self.logic.blocks.get("02Msg_Stn") or {"nets": []})["nets"]:
            for x in w.get("w", []):
                if x["o"] == "Err":
                    return txt(x.get("c"))
        return ""

    def stats(self, alarms: dict[int, dict[str, Any]], catalog: dict[int, str]) -> dict[str, Any]:
        mapped = [e for e in alarms.values() if e["by"] != "none"]
        with_text = [e for e in alarms.values() if e.get("text")]
        return {"records": len(self.recs), "mapped_bits": len(mapped), "catalog_with_text": len([1 for t in catalog.values() if t.strip()]),
                "text_mapped": len([e for e in with_text if e["by"] != "none"]), "text_confirmed": len([e for e in with_text if e["by"] == "text"]),
                "text_unmapped": len([e for e in with_text if e["by"] == "none"]),
                "empty_slots_mapped": len([e for e in mapped if not e.get("text")]),
                "tainted": len([e for e in mapped if any("ненадёжен" in f for f in e.get("flags", []))])}


STOP = {"alarm", "alarms", "error", "fault", "the", "and", "for", "when", "not", "its", "has", "have", "with", "from", "this", "that",
        "station", "signal", "device", "lost", "motor"}


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokens(s: str) -> set[str]:
    res = set()
    for m in re.finditer(r"[a-z]+[0-9]*(?:\.[0-9]+)*|[0-9]+", s.lower()):
        t = re.sub(r"(?<=[a-z])0+(?=\d)", "", m.group(0))
        if len(t) >= 2 and t not in STOP:
            res.add(t)
    return res


def _path(fr: Frame, rec: dict[str, Any]) -> str:
    """Цепочка вызовов от OB1: «блок [экземпляр] (сеть N) → ...»; сеть N — сеть этого блока, из которой вызван следующий."""
    chain = fr.chain()
    parts = []
    for i, f in enumerate(chain):
        net = chain[i + 1].net if i + 1 < len(chain) else rec["net"]
        parts.append(f"{f.block['name']}" + (f" [{f.inst}]" if f.inst else "") + f" (сеть {net})")
    return " → ".join(parts + [rec["msgblk"]])


def _single_var(node: Any) -> bool:
    return isinstance(node, dict) and "_x" in node and not node.get("n") and set(node) <= {"v", "_x"}


def _xleaves(node: Any) -> list[dict[str, Any]]:
    res: list[dict[str, Any]] = []
    if isinstance(node, list):
        for x in node:
            res.extend(_xleaves(x))
    elif isinstance(node, dict):
        if "_x" in node:
            res.append(node)
        for k, v in node.items():
            if k not in ("_x", "v") and isinstance(v, (dict, list)):
                res.extend(_xleaves(v))
    return res


def build_map(logic: T.TiaLogic, catalog: dict[int, str] | None = None, built_from: str = "") -> dict[str, Any]:
    """Карта аварий для проекта: обход от Main (OB1). Исключение, если в выгрузке нет блока Main."""
    m = AlarmMap(logic)
    m.run("Main")
    return m.build(catalog, built_from)


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Карта аварий ПЛК из выгрузки TIA Portal")
    ap.add_argument("path", help="ZIP или папка выгрузки, либо JSON логики (результат tia_logic.py -o)")
    ap.add_argument("-o", "--out", help="файл карты alarms.json")
    ap.add_argument("--bit", type=int, help="показать одну аварию по номеру бита")
    a = ap.parse_args(argv)
    p = Path(a.path)
    logic = T.TiaLogic.from_json(json.loads(p.read_text(encoding="utf-8"))) if p.is_file() and p.suffix.lower() == ".json" else T.TiaLogic.load(p)
    data = build_map(logic, None, built_from=p.name)
    if a.out:
        Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{a.out}: {Path(a.out).stat().st_size} байт")
    print(json.dumps(data["stats"], ensure_ascii=False))
    if a.bit is not None:
        e = data["alarms"].get(str(a.bit))
        print(json.dumps(e, ensure_ascii=False, indent=1) if e else f"бит {a.bit}: источник не найден")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
