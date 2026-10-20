"""Логика ПЛК из выгрузки TIA Portal (SimaticML XML, сети LAD/FBD) в компактном виде «выход -> цепочка условий».

Нужна ИИ-помощнику «почему стоит станция» (S-7): локальная модель сырой XML не читает, поэтому код разбирает блоки FC/FB/OB
в дерево условий И/ИЛИ, дальше (S-8) по дереву и текущим значениям тегов считается невыполненное условие, а картинка контактов
(S-9) рисуется из того же результата. Только разбор файлов, без БД и без связи с ПЛК. Запуск как скрипт:
    python tia_logic.py <ZIP или папка выгрузки> [-o logic.json] [--chain ИМЯ_ВЫХОДА] [--depth N] [--stats]

Как устроено:
- проводка сети: у каждого <Wire> первое соединение источник (Powerrail, IdentCon = переменная/константа, NameCon = выходной
  контакт элемента), остальные приёмники; значение входа элемента берётся рекурсивно от источника, Powerrail = «всегда»;
- у каждого выхода (катушка, Move, выход вызова блока, выход элемента с переменной) запись {o: имя, k: вид, c: условие, v: значение};
- имя переменной как в TIA: путь через точку, части с пробелом или знаками в кавычках (ITL_GWM_DB."From GWM"); локальные переменные
  блока (параметры, Temp, Static) помечены l=1;
- узлы условия (короткие ключи): {v:имя, n:1 NC, l:1}, {and:[..]}, {or:[..]}, {not:x}, {cmp:"==", a, b}, {k:константа},
  {timer:"TON", inst, in, pt}, {edge:"P"/"N", v, mem}, {range:"in"/"out", min, v, max}, {fn:"Add", a:[..]}, {sr, s, r, v}, {ctr, ...},
  {call:uid, p:выход} (ссылка на вызов блока в этой сети; в ответах chains_for раскрывается в {call:блок, inst, args, body});
- что не разобрано (Calc, строковые функции, системные вызовы, Jump, SCL/STL-сети) явно: {"unparsed": тип} в условии и список u в сети;
  ничего не выдумывается."""
from __future__ import annotations

import copy
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# --- Справочники элементов LAD/FBD ---
COIL_KINDS = {"Coil": "coil", "SCoil": "set", "RCoil": "reset", "PCoil": "coil_p", "NCoil": "coil_n"}
CMP_OPS = {"Eq": "==", "Ne": "<>", "Gt": ">", "Ge": ">=", "Lt": "<", "Le": "<="}
TIMERS = {"TON", "TOF", "TONR", "TP"}
COUNTERS = {"CTU", "CTD", "CTUD"}
FN_BOXES = {"Add", "Sub", "Mul", "Div", "Convert", "Round", "Shr", "Shl", "Normalize", "Scale_X", "Abs"}
HANDLED = (set(COIL_KINDS) | set(CMP_OPS) | TIMERS | COUNTERS | FN_BOXES
           | {"Contact", "NContact", "PContact", "O", "A", "X", "Not", "InRange", "OutRange", "Move", "S_Move",
              "Sr", "Rs", "PBox", "NBox", "Inc", "Dec"})
EDGE_CONTACTS = {"PContact": "P", "NContact": "N"}
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LANG_TITLE = ("ru-RU", "en-US", "kk-KZ")


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _ln(c.tag) == name]


def _kid(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _q(part: str) -> str:
    return part if IDENT_RE.match(part) else '"' + part + '"'


def split_name(name: str) -> list[str]:
    """Имя переменной на части по точкам вне кавычек и скобок индекса; кавычки в частях убираются, индекс остаётся в части."""
    out, cur, inq, depth = [], [], False, 0
    for ch in name:
        if ch == '"':
            inq = not inq
        elif ch == "[" and not inq:
            depth += 1
            cur.append(ch)
        elif ch == "]" and not inq:
            depth -= 1
            cur.append(ch)
        elif ch == "." and not inq and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def norm_name(name: str) -> str:
    """Ключ поиска: без кавычек, без различия регистра и пробелов по краям частей."""
    return ".".join(p.strip().casefold() for p in split_name(name))


def _root(name: str) -> str:
    return re.sub(r"\[.*$", "", split_name(name)[0])


# --- Операции над деревьями условий ---
def _and(*xs: Any) -> Any:
    items: list[Any] = []
    for x in xs:
        if x is None:
            continue
        if isinstance(x, dict) and "and" in x:
            items.extend(x["and"])
        else:
            items.append(x)
    seen, uniq = set(), []
    for x in items:
        key = json.dumps(x, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    if not uniq:
        return None
    return uniq[0] if len(uniq) == 1 else {"and": uniq}


def _or(*xs: Any) -> Any:
    items: list[Any] = []
    for x in xs:
        if x is None:
            return None             # одна ветка «всегда» делает ИЛИ истинным
        if isinstance(x, dict) and "or" in x:
            items.extend(x["or"])
        else:
            items.append(x)
    seen, uniq = set(), []
    for x in items:
        key = json.dumps(x, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq[0] if len(uniq) == 1 else {"or": uniq}


def _not(x: Any) -> Any:
    if x is None:
        return {"k": "FALSE"}
    if "v" in x:
        y = dict(x)
        if y.get("n"):
            y.pop("n")
        else:
            y["n"] = 1
        return y
    if "not" in x:
        return x["not"]
    return {"not": x}


# --- Разбор одной сети (FlgNet) ---
class _Net:
    """Граф одной сети: элементы, переменные, провода; значение выхода элемента считается рекурсивно с запоминанием."""

    def __init__(self, flg: ET.Element) -> None:
        self.parts: dict[str, dict[str, Any]] = {}
        self.acc: dict[str, dict[str, Any]] = {}
        self.src: dict[tuple[str, str], tuple] = {}         # (элемент, вход) -> источник
        self.sinks: dict[tuple[str, str], list[str]] = {}   # (элемент, выход) -> uid переменных-приёмников
        self.order: list[str] = []
        self.calls: dict[str, dict[str, Any]] = {}
        self.unparsed: list[str] = []
        self._memo: dict[tuple[str, str], Any] = {}
        self._busy: set[tuple[str, str]] = set()
        parts = _kid(flg, "Parts")
        for e in parts if parts is not None else []:
            t = _ln(e.tag)
            uid = e.get("UId") or ""
            if t == "Access":
                self.acc[uid] = self._access(e)
            elif t == "Part":
                neg = {n.get("Name") for n in _kids(e, "Negated")}
                inst = _kid(e, "Instance")
                self.parts[uid] = {"name": e.get("Name"), "neg": neg, "inst": self._symbol(inst) if inst is not None else None}
                self.order.append(uid)
            elif t == "Call":
                self._call(uid, e)
        wires = _kid(flg, "Wires")
        for w in wires if wires is not None else []:
            cs = list(w)
            if not cs:
                continue
            f = cs[0]
            ft = _ln(f.tag)
            if ft == "Powerrail":
                s: tuple = ("power",)
            elif ft == "IdentCon":
                s = ("acc", f.get("UId"))
            elif ft == "NameCon":
                s = ("pin", f.get("UId"), f.get("Name"))
            else:
                s = ("open",)
            for c in cs[1:]:
                ct = _ln(c.tag)
                if ct == "NameCon":
                    self.src[(c.get("UId") or "", c.get("Name") or "")] = s
                elif ct == "IdentCon" and s[0] == "pin":
                    self.sinks.setdefault((s[1], s[2]), []).append(c.get("UId") or "")

    # -- переменные --
    def _symbol(self, holder: ET.Element) -> str:
        sym = _kid(holder, "Symbol")
        comps = _kids(sym, "Component") if sym is not None else _kids(holder, "Component")
        parts = []
        for c in comps:
            s = _q(c.get("Name") or "")
            idx = [self._const_text(a) for a in _kids(c, "Access")]
            if idx:
                s += "[" + ",".join(idx) + "]"
            parts.append(s)
        return ".".join(parts)

    def _const_text(self, a: ET.Element) -> str:
        if a.get("Scope") in ("LiteralConstant", "TypedConstant"):
            cv = a.find(".//{*}ConstantValue")
            return (cv.text or "") if cv is not None else ""
        return self._symbol(a) if _kid(a, "Symbol") is not None else "?"

    def _access(self, e: ET.Element) -> dict[str, Any]:
        sc = e.get("Scope") or ""
        if sc in ("GlobalVariable", "LocalVariable"):
            node: dict[str, Any] = {"v": self._symbol(e)}
            if sc == "LocalVariable":
                node["l"] = 1
            return node
        if sc in ("LiteralConstant", "TypedConstant"):
            return {"k": self._const_text(e)}
        if sc in ("GlobalConstant", "LocalConstant"):
            return {"k": self._symbol(e)}
        self.unparsed.append("Access:" + sc)
        return {"unparsed": "Access:" + sc}

    def _call(self, uid: str, e: ET.Element) -> None:
        ci = _kid(e, "CallInfo")
        if ci is None:
            self.unparsed.append("Call")
            return
        inst = _kid(ci, "Instance")
        params = [{"name": p.get("Name"), "sec": p.get("Section")} for p in _kids(ci, "Parameter")]
        self.calls[uid] = {"blk": ci.get("Name"), "type": ci.get("BlockType"), "inst": self._symbol(inst) if inst is not None else None, "params": params}
        self.parts[uid] = {"name": "Call", "neg": set(), "inst": None}
        self.order.append(uid)

    # -- значения --
    def inp(self, uid: str, pin: str) -> Any:
        """Значение входа элемента: None = «всегда» (шина питания) или не подключено."""
        s = self.src.get((uid, pin))
        if s is None or s[0] == "open":
            return None
        if s[0] == "power":
            v = None
        elif s[0] == "acc":
            v = copy.deepcopy(self.acc.get(s[1], {"unparsed": "Access"}))
        else:
            v = self.out(s[1], s[2])
        if pin in self.parts.get(uid, {}).get("neg", ()):
            v = _not(v)
        return v

    def connected(self, uid: str, pin: str) -> bool:
        s = self.src.get((uid, pin))
        return s is not None and s[0] != "open"

    def _pins(self, uid: str, prefix: str) -> list[str]:
        pins = [p for (u, p) in self.src if u == uid and re.fullmatch(prefix + r"\d+", p)]
        return sorted(pins, key=lambda p: int(p[len(prefix):]))

    def out(self, uid: str, pin: str) -> Any:
        key = (uid, pin)
        if key in self._memo:
            return copy.deepcopy(self._memo[key])
        if key in self._busy:
            return {"unparsed": "loop"}
        self._busy.add(key)
        try:
            v = self._out(uid, pin)
        finally:
            self._busy.discard(key)
        self._memo[key] = v
        return copy.deepcopy(v)

    def _out(self, uid: str, pin: str) -> Any:
        p = self.parts.get(uid)
        if p is None:
            return {"unparsed": "?"}
        n = p["name"]
        if n == "Call":
            return self._call_out(uid, pin)
        if pin == "eno":
            return self.inp(uid, "en")
        if n == "Contact":
            return _and(self.inp(uid, "in"), self.inp(uid, "operand"))
        if n in EDGE_CONTACTS:
            return _and(self.inp(uid, "pre"), {"edge": EDGE_CONTACTS[n], "v": self.inp(uid, "operand"), "mem": self.inp(uid, "bit")})
        if n in ("PBox", "NBox"):
            return _and(self.inp(uid, "in"), {"edge": "P" if n == "PBox" else "N", "v": self.inp(uid, "bit")})
        if n in COIL_KINDS:
            return self.inp(uid, "in")
        if n == "Not":
            return _not(self.inp(uid, "in"))
        if n == "O":
            return _or(*[self.inp(uid, q) for q in self._pins(uid, "in")])
        if n == "A":
            return _and(*[self.inp(uid, q) for q in self._pins(uid, "in")])
        if n == "X":
            return {"xor": [self.inp(uid, q) for q in self._pins(uid, "in")]}
        if n in CMP_OPS:
            return _and(self.inp(uid, "pre"), {"cmp": CMP_OPS[n], "a": self.inp(uid, "in1"), "b": self.inp(uid, "in2")})
        if n in ("InRange", "OutRange"):
            return _and(self.inp(uid, "pre"), {"range": "in" if n == "InRange" else "out", "min": self.inp(uid, "min"),
                                               "v": self.inp(uid, "in"), "max": self.inp(uid, "max")})
        if n in TIMERS:
            node = {"timer": n, "inst": p["inst"], "in": self.inp(uid, "IN"), "pt": self.inp(uid, "PT")}
            if n == "TONR":
                node["r"] = self.inp(uid, "R")
            return node
        if n in COUNTERS:
            return {"ctr": n, "inst": p["inst"], "cu": self.inp(uid, "CU"), "cd": self.inp(uid, "CD"), "r": self.inp(uid, "R"),
                    "pv": self.inp(uid, "PV")}
        if n in ("Sr", "Rs"):
            s_pin, r_pin = ("s", "r1") if n == "Sr" else ("s1", "r")
            return {"sr": n, "s": self.inp(uid, s_pin), "r": self.inp(uid, r_pin), "v": self.inp(uid, "operand")}
        if n in ("Move", "S_Move"):
            return self.inp(uid, "in")
        if n in FN_BOXES:
            ins = [self.inp(uid, q) for q in self._pins(uid, "in")] or [self.inp(uid, q) for q in ("in", "value", "operand") if self.connected(uid, q)]
            return {"fn": n, "a": ins}
        return {"unparsed": n}

    def _call_out(self, uid: str, pin: str) -> Any:
        if pin == "eno":
            return self.inp(uid, "en")
        return {"call": uid, "p": pin}

    # -- записи сети --
    def writes(self) -> list[dict[str, Any]]:
        res: list[dict[str, Any]] = []
        for uid in self.order:
            p = self.parts[uid]
            n = p["name"]
            if n in COIL_KINDS:
                for a in self._targets(uid, "operand"):
                    cond = self.inp(uid, "in")
                    kind = COIL_KINDS[n]
                    if n == "Coil" and "operand" in p["neg"]:
                        kind = "coil_neg"
                        cond = self.inp(uid, "in")
                    res.append(self._w(a, kind, cond))
            elif n in ("Sr", "Rs"):
                for a in self._targets(uid, "operand"):
                    node = self._out(uid, "q")
                    res.append(self._w(a, n.lower(), None, value={k: v for k, v in node.items() if k in ("s", "r")}))
            elif n in ("Inc", "Dec"):
                for a in self._targets(uid, "operand"):
                    res.append(self._w(a, n.lower(), self.inp(uid, "en")))
            elif n in TIMERS or n in COUNTERS:
                node = self._out(uid, "Q")
                res.append(self._w(p["inst"] or "?", "timer" if n in TIMERS else "counter", node.get("in") if n in TIMERS else node.get("cu"),
                                   value={k: v for k, v in node.items() if k in ("timer", "ctr", "pt", "r", "pv", "cd")}))
                for pin in ("Q", "QU", "QD"):
                    for a in self.sinks.get((uid, pin), []):
                        res.append(self._w(self.acc[a], "out", None, value=self._out(uid, pin)))
            elif n == "Call":
                c = self.calls[uid]
                for prm in c["params"]:
                    if prm["sec"] in ("Output", "InOut", "Return"):
                        for a in self.sinks.get((uid, prm["name"]), []):
                            w = self._w(self.acc[a], "callout", self.inp(uid, "en"))
                            w["call"], w["p"] = uid, prm["name"]
                            res.append(w)
            elif n in HANDLED and n not in ("Contact", "NContact", "PContact", "O", "A", "X", "Not", "PBox", "NBox") and n not in CMP_OPS \
                    and n not in ("InRange", "OutRange"):
                # Move, S_Move, вычисления: выход с переменной-приёмником
                for (u, pin), accs in self.sinks.items():
                    if u != uid:
                        continue
                    for a in accs:
                        kind = "move" if n in ("Move", "S_Move") else "out"
                        res.append(self._w(self.acc[a], kind, self.inp(uid, "en"), value=self.out(uid, pin) if kind == "out" else self.inp(uid, "in")))
            elif n not in HANDLED:
                self.unparsed.append(n)
                for (u, pin), accs in self.sinks.items():
                    if u == uid:
                        for a in accs:
                            res.append(self._w(self.acc[a], "out", self.inp(uid, "en"), value={"unparsed": n}))
        return res

    def _targets(self, uid: str, pin: str) -> list[dict[str, Any]]:
        s = self.src.get((uid, pin))
        if s is None or s[0] != "acc":
            return []
        return [self.acc.get(s[1], {"unparsed": "Access"})]

    @staticmethod
    def _w(target: Any, kind: str, cond: Any, value: Any = None) -> dict[str, Any]:
        t = target if isinstance(target, dict) else {"v": target}
        w: dict[str, Any] = {"o": t.get("v") or t.get("k") or "?", "k": kind}
        if t.get("l"):
            w["l"] = 1
        if cond is not None:
            w["c"] = cond
        if value is not None:
            w["v"] = value
        return w

    def call_nodes(self) -> dict[str, dict[str, Any]]:
        res = {}
        for uid, c in self.calls.items():
            args = {}
            for prm in c["params"]:
                if prm["sec"] in ("Input", "InOut") and self.connected(uid, prm["name"]):
                    args[prm["name"]] = self.inp(uid, prm["name"])
            node = {"blk": c["blk"], "type": c["type"]}
            if c["inst"]:
                node["inst"] = c["inst"]
            en = self.inp(uid, "en")
            if en is not None:
                node["en"] = en
            node["args"] = args
            res[uid] = node
        return res


# --- Разбор блока ---
def _title(cu: ET.Element) -> str:
    texts: dict[str, str] = {}
    ol = _kid(cu, "ObjectList")
    for m in _kids(ol, "MultilingualText") if ol is not None else []:
        if m.get("CompositionName") == "Title":
            for it in m.iter():
                if _ln(it.tag) == "MultilingualTextItem":
                    al = _kid(it, "AttributeList")
                    cul = _kid(al, "Culture") if al is not None else None
                    tx = _kid(al, "Text") if al is not None else None
                    if cul is not None and tx is not None and (tx.text or "").strip():
                        texts[cul.text or ""] = tx.text.strip()
    for lang in LANG_TITLE:
        if lang in texts:
            return texts[lang]
    return next(iter(texts.values()), "")


def parse_block(root: ET.Element) -> dict[str, Any] | None:
    """Блок FC/FB/OB из корня XML выгрузки; БД и прочее возвращают None."""
    blk = None
    for c in root:
        if _ln(c.tag) in ("SW.Blocks.FC", "SW.Blocks.FB", "SW.Blocks.OB"):
            blk = c
    if blk is None:
        return None
    al = _kid(blk, "AttributeList")
    g = lambda n: (_kid(al, n).text or "") if al is not None and _kid(al, n) is not None else ""   # noqa: E731
    ifc = _kid(al, "Interface") if al is not None else None
    out: dict[str, Any] = {"name": g("Name"), "num": int(g("Number") or 0), "type": _ln(blk.tag)[10:], "lang": g("ProgrammingLanguage")}
    # интерфейс верхнего уровня (вложенные структуры не разворачиваются)
    top: dict[str, list[list[str]]] = {}
    if ifc is not None:
        secs = [s for s in ifc.iter() if _ln(s.tag) == "Sections"]
        if secs:
            for sec in _kids(secs[0], "Section"):
                mem = [[m.get("Name") or "", (m.get("Datatype") or "")] for m in _kids(sec, "Member")]
                if mem:
                    top[sec.get("Name") or ""] = mem
    out["if"] = top
    nets = []
    ol = _kid(blk, "ObjectList")
    idx = 0
    for cu in _kids(ol, "SW.Blocks.CompileUnit") if ol is not None else []:
        idx += 1
        net: dict[str, Any] = {"n": idx}
        title = _title(cu)
        if title:
            net["t"] = title
        src_holder = _kid(_kid(cu, "AttributeList"), "NetworkSource") if _kid(cu, "AttributeList") is not None else None
        flg = next((e for e in src_holder if _ln(e.tag) == "FlgNet"), None) if src_holder is not None else None
        if flg is None:
            kind = "SCL" if src_holder is not None and src_holder.find(".//{*}StructuredText") is not None else \
                ("STL" if src_holder is not None and src_holder.find(".//{*}StatementList") is not None else "пусто")
            if kind != "пусто":
                net["u"] = [kind]
                nets.append(net)
            continue
        gnet = _Net(flg)
        w = gnet.writes()
        if w:
            net["w"] = w
        calls = gnet.call_nodes()
        if calls:
            net["calls"] = calls
        if gnet.unparsed:
            net["u"] = gnet.unparsed
        if w or calls or gnet.unparsed:
            nets.append(net)
    out["nets"] = nets
    return out


# --- Проект ---
class TiaLogic:
    """Логика проекта: блоки, индекс выходов, цепочки и раскрытие вызовов."""

    def __init__(self, blocks: dict[str, dict[str, Any]], plc: str = "") -> None:
        self.blocks = blocks
        self.plc = plc
        self.index: dict[str, list[tuple[str, int, int]]] = {}
        self.instances: dict[str, tuple[str, str, int, str]] = {}   # имя экземпляра -> (FB, блок вызова, индекс сети, uid вызова)
        self._build_index()

    # -- загрузка --
    @classmethod
    def load(cls, path: str | Path) -> "TiaLogic":
        """ZIP или папка выгрузки (как делает tia-export.ps1); читаются только .xml блоков."""
        p = Path(path)
        blocks: dict[str, dict[str, Any]] = {}
        plc = ""

        def add(data: bytes, where: str) -> None:
            if b"SW.Blocks." not in data:
                return
            try:
                b = parse_block(ET.fromstring(data))
            except ET.ParseError:
                return
            if b and b["name"]:
                b["file"] = where
                blocks[b["name"]] = b

        if p.is_file():
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    n2 = n.replace("\\", "/")
                    if n2.lower().endswith(".xml"):
                        add(z.read(n), n2.split("/", 1)[-1] if "/" in n2 else n2)
                        m = re.search(r"/([^/]+)/Blocks/", "/" + n2)
                        if m:
                            plc = m.group(1)
        else:
            for f in sorted(p.rglob("*.xml")):
                add(f.read_bytes(), f.relative_to(p).as_posix())
                m = re.search(r"/([^/]+)/Blocks/", "/" + f.relative_to(p).as_posix())
                if m:
                    plc = m.group(1)
        return cls(blocks, plc)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TiaLogic":
        return cls(data["blocks"], data.get("plc", ""))

    # -- индекс --
    def _build_index(self) -> None:
        for bname, b in self.blocks.items():
            for ni, net in enumerate(b["nets"]):
                for wi, w in enumerate(net.get("w", [])):
                    self.index.setdefault(norm_name(w["o"]), []).append((bname, ni, wi))
                for uid, c in net.get("calls", {}).items():
                    if c.get("inst") and c.get("type") == "FB":
                        self.instances.setdefault(norm_name(c["inst"]), (c["blk"], bname, ni, uid))

    # -- вывод --
    def to_dict(self) -> dict[str, Any]:
        return {"plc": self.plc, "blocks": self.blocks, "stats": self.stats()}

    def to_json(self, path: str | Path | None = None) -> str:
        s = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if path:
            Path(path).write_text(s, encoding="utf-8")
        return s

    def stats(self) -> dict[str, Any]:
        un: dict[str, int] = {}
        nets = writes = 0
        by_type: dict[str, int] = {}
        for b in self.blocks.values():
            by_type[b["type"]] = by_type.get(b["type"], 0) + 1
            for net in b["nets"]:
                nets += 1
                writes += len(net.get("w", []))
                for u in net.get("u", []):
                    un[u] = un.get(u, 0) + 1
        return {"blocks": by_type, "nets": nets, "writes": writes, "unparsed": dict(sorted(un.items(), key=lambda kv: -kv[1]))}

    # -- цепочки --
    def _entry(self, bname: str, ni: int, wi: int) -> dict[str, Any]:
        b = self.blocks[bname]
        net = b["nets"][ni]
        return {"block": bname, "net": net["n"], "title": net.get("t", ""), **copy.deepcopy(net["w"][wi])}

    def chains_for(self, output_name: str, depth: int = 0, partial: bool = False) -> list[dict[str, Any]]:
        """Все места, где записывается выход (катушки, Move, выходы вызовов): блок, сеть, вид, условие c, значение v.
        depth > 0 раскрывает вызовы блоков внутрь (call -> {blk, inst, p, args, body}); name без префикса БД с partial=True
        ищет по окончанию имени (Goto3rd -> OPMode08_DB.EL300.Goto3rd)."""
        key = norm_name(output_name)
        hits = list(self.index.get(key, []))
        if not hits and partial:
            for k, v in self.index.items():
                if k == key or k.endswith("." + key):
                    hits.extend(v)
        res = []
        for bname, ni, wi in hits:
            e = self._entry(bname, ni, wi)
            res.append(self._expand_entry(e, bname, ni, depth))
        if not res:
            res = self._instance_chains(output_name, depth)
        return res

    def _instance_chains(self, name: str, depth: int) -> list[dict[str, Any]]:
        """Выход экземпляра FB (ИмяDB.Выход): записи внутри FB, параметры подставлены из вызова."""
        parts = split_name(name)
        for cut in range(len(parts) - 1, 0, -1):
            inst = ".".join(_q(p) for p in parts[:cut])
            ref = self.instances.get(norm_name(inst))
            if ref:
                rest = ".".join(parts[cut:])
                return self._callee_writes(ref[1], ref[2], ref[3], rest, depth)
        return []

    def _callee_writes(self, bname: str, ni: int, uid: str, member: str, depth: int) -> list[dict[str, Any]]:
        call = self.blocks[bname]["nets"][ni]["calls"][uid]
        callee = self.blocks.get(call["blk"])
        if callee is None:
            return []
        env = self._env(callee, call)
        want = norm_name(member)
        res = []
        for net in callee["nets"]:
            for w in net.get("w", []):
                if w.get("l") and (norm_name(w["o"]) == want or norm_name(w["o"]).startswith(want + ".")):
                    e = {"block": callee["name"], "net": net["n"], "title": net.get("t", ""), **copy.deepcopy(w)}
                    e = self._subst(e, env)
                    e["o"] = (call.get("inst") or "") + "." + w["o"] if call.get("inst") else w["o"]
                    e.pop("l", None)
                    res.append(self._expand_entry(e, callee["name"], net["n"] - 1, depth))
        return res

    def _env(self, callee: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
        """Подстановка параметров: Input/InOut -> фактический аргумент; Static/Output -> путь экземпляра."""
        env: dict[str, Any] = {"inst": call.get("inst"), "args": call.get("args", {}), "static": set()}
        for sec in ("Static", "Output", "InOut", "Input", "Return"):
            for m in callee["if"].get(sec, []):
                if sec in ("Static", "Output", "Return") or m[0] not in env["args"]:
                    env["static"].add(norm_name(m[0]))
        return env

    def _subst(self, node: Any, env: dict[str, Any]) -> Any:
        if isinstance(node, list):
            return [self._subst(x, env) for x in node]
        if not isinstance(node, dict):
            return node
        if "v" in node and node.get("l") and isinstance(node["v"], str):
            parts = split_name(node["v"])
            root = _root(node["v"])
            arg = env["args"].get(root)
            if arg is not None and "v" in arg and root not in env["static"]:
                res = dict(arg)
                if len(parts) > 1:
                    res["v"] = arg["v"] + "." + ".".join(_q(p) for p in parts[1:])
                if node.get("n"):
                    res = _not(res)
                return res
            if arg is not None and len(parts) == 1 and not node.get("n"):
                return copy.deepcopy(arg)
            if norm_name(root) in env["static"] and env.get("inst"):
                res = dict(node)
                res["v"] = env["inst"] + "." + ".".join(_q(p) for p in parts)
                res.pop("l", None)
                return res
            return node
        return {k: (self._subst(v, env) if k not in ("o",) else v) for k, v in node.items()}

    def _expand_entry(self, e: dict[str, Any], bname: str, ni: int, depth: int) -> dict[str, Any]:
        net = self.blocks[bname]["nets"][ni] if bname in self.blocks and ni < len(self.blocks[bname]["nets"]) else {"calls": {}}
        calls = net.get("calls", {})
        for key in ("c", "v"):
            if key in e:
                e[key] = self._expand(e[key], calls, depth)
        if e.get("k") == "callout" and "call" in e:
            e["call"] = self._call_view(calls, e["call"], e.pop("p"), depth)
        return e

    def _expand(self, node: Any, calls: dict[str, Any], depth: int) -> Any:
        if isinstance(node, list):
            return [self._expand(x, calls, depth) for x in node]
        if not isinstance(node, dict):
            return node
        if "call" in node and isinstance(node["call"], str) and "p" in node:
            return {"call": self._call_view(calls, node["call"], node["p"], depth)}
        return {k: self._expand(v, calls, depth) for k, v in node.items()}

    def _call_view(self, calls: dict[str, Any], uid: str, pin: str, depth: int) -> dict[str, Any]:
        c = calls.get(uid)
        if c is None:
            return {"unparsed": "Call"}
        view: dict[str, Any] = {k: v for k, v in c.items() if k != "type"}
        view["p"] = pin
        if depth <= 0:
            return view
        callee = self.blocks.get(c["blk"])
        if callee is None:
            view["body"] = "блок не найден в выгрузке"
            return view
        env = self._env(callee, c)
        body = []
        want = norm_name(pin)
        for net in callee["nets"]:
            for w in net.get("w", []):
                if w.get("l") and (norm_name(w["o"]) == want or norm_name(w["o"]).startswith(want + ".")):
                    e = {"net": net["n"], "title": net.get("t", ""), **copy.deepcopy(w)}
                    e = self._subst(e, env)
                    e.pop("l", None)
                    body.append(self._expand_entry(e, callee["name"], net["n"] - 1, depth - 1))
        view["body"] = body
        return view


# --- Читаемый вид ---
def to_text(node: Any, top: bool = True) -> str:
    """Условие одной строкой для проверки человеком и подсказки модели: (A ИЛИ B) И C И НЕ D."""
    if node is None:
        return "ВСЕГДА"
    if "v" in node and len(node) <= 3 and "k" not in node and "edge" not in node and "range" not in node and "sr" not in node:
        return ("НЕ " if node.get("n") else "") + node["v"]
    if "k" in node:
        return str(node["k"])
    if "and" in node:
        s = " И ".join(to_text(x, False) for x in node["and"])
        return s if top else "(" + s + ")"
    if "or" in node:
        s = " ИЛИ ".join(to_text(x, False) for x in node["or"])
        return s if top else "(" + s + ")"
    if "not" in node:
        return "НЕ (" + to_text(node["not"], True) + ")"
    if "cmp" in node:
        return f"({to_text(node['a'], False)} {node['cmp']} {to_text(node['b'], False)})"
    if "timer" in node:
        return f"{node['timer']}({node.get('inst')}: {to_text(node.get('in'))}, {to_text(node.get('pt'))})"
    if "edge" in node:
        return f"фронт{node['edge']}({to_text(node.get('v'))})"
    if "range" in node:
        return f"{node['range']}Range({to_text(node.get('min'))} .. {to_text(node.get('v'))} .. {to_text(node.get('max'))})"
    if "fn" in node:
        return node["fn"] + "(" + ", ".join(to_text(x, False) for x in node["a"]) + ")"
    if "call" in node:
        c = node["call"]
        return f"{c.get('blk')}.{c.get('p')}" if isinstance(c, dict) else f"вызов#{c}"
    if "unparsed" in node:
        return f"[не разобрано: {node['unparsed']}]"
    return json.dumps(node, ensure_ascii=False)


def leaf_names(node: Any) -> set[str]:
    """Имена всех переменных в дереве условия (без учёта инверсии)."""
    res: set[str] = set()
    if isinstance(node, list):
        for x in node:
            res |= leaf_names(x)
    elif isinstance(node, dict):
        if isinstance(node.get("v"), str):
            res.add(node["v"])
        for k, v in node.items():
            if k != "v" and isinstance(v, (dict, list)):
                res |= leaf_names(v)
    return res


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Разбор логики ПЛК из выгрузки TIA Portal в JSON")
    ap.add_argument("path", help="ZIP или папка выгрузки")
    ap.add_argument("-o", "--out", help="файл JSON для всего проекта")
    ap.add_argument("--chain", help="имя выхода: показать цепочки")
    ap.add_argument("--depth", type=int, default=0, help="глубина раскрытия вызовов блоков")
    ap.add_argument("--stats", action="store_true", help="сводка по блокам и нераспознанному")
    a = ap.parse_args(argv)
    t = TiaLogic.load(a.path)
    if a.out:
        t.to_json(a.out)
        print(f"{a.out}: {Path(a.out).stat().st_size} байт")
    if a.stats:
        print(json.dumps(t.stats(), ensure_ascii=False, indent=1))
    if a.chain:
        for e in t.chains_for(a.chain, a.depth, partial=True):
            print(f"{e['block']} сеть {e['net']} {e['k']} {e['o']} <= {to_text(e.get('c'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
