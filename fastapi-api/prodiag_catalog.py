"""Справочник аварий ProDiag из выгрузки TIA Portal (SimaticML XML): все контроли с абсолютными адресами OPC UA.

Только разбор файлов, без БД и без связи с ПЛК. Запуск как скрипт строит файл Excel для проверки человеком:
    python prodiag_catalog.py <папка выгрузки ПЛК> <файл .xlsx> [ProjectTexts.xlsx]

Как устроено:
- типы FB с контролями: <Supervisions><BlockTypeSupervisions> в XML блока FB;
- экземпляры: вызовы <CallInfo Name=тип> (Instance Scope=GlobalVariable: экземплярный DB, LocalVariable: мультиэкземпляр
  внутри FB-родителя, путь родителя + имя) и экземплярные DB из выгрузки; родитель может вызываться много раз, раскрываются все пути;
- операнд "#X.Y" экземпляра "DB"."Inst" даёт узел ns=3;s="DB"."Inst"."X"."Y"; индексы массивов пересчитываются с нуля
  (Array[1..16]: элемент [15] в TIA это узел [14]), глобальные операнды ("DB".a.b) берутся как есть;
- фактические параметры вызова (что подключено к входу FB в LAD/FBD/SCL) тоже выводятся: члены многих библиотечных FB
  закрыты для OPC UA (ExternalAccessible=false), и тогда читать можно только подключённый глобальный сигнал.
"""
from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

PLC_NAME = "WBS1"
GENERAL_SOURCE = "General_PD"
LANGS = ("en-US", "ru-RU", "kk-KZ", "zh-CN", "de-DE")


def _local(tag: str) -> str:
    """Имя тега XML без пространства имён."""
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _iter(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            yield c


def _texts(el) -> dict[str, str]:
    """MultiLanguageText → {язык: текст} (пустые пропускаются)."""
    out = {}
    if el is None:
        return out
    for t in _iter(el, "MultiLanguageText"):
        if (t.text or "").strip():
            out[t.get("Lang", "")] = t.text.strip()
    return out


def pick_text(texts: dict[str, str]) -> str:
    """Текст на первом доступном языке: английский, русский, казахский, китайский."""
    for lang in LANGS:
        if texts.get(lang):
            return texts[lang]
    return next(iter(texts.values()), "")


# ---------------------------------------------------------------- интерфейс (члены блоков и типов)

@dataclass
class Member:
    name: str
    datatype: str
    section: str = ""
    comment: dict = field(default_factory=dict)
    accessible: bool = True
    children: dict = field(default_factory=dict)


def _parse_members(parent, section: str) -> dict[str, Member]:
    out: dict[str, Member] = {}
    for m in _children(parent, "Member"):
        acc = True
        attrs = _child(m, "AttributeList")
        if attrs is not None:
            for b in _children(attrs, "BooleanAttribute"):
                if b.get("Name") == "ExternalAccessible" and (b.text or "").strip().lower() == "false":
                    acc = False
        mem = Member(m.get("Name", ""), m.get("Datatype", ""), section, _texts(_child(m, "Comment")), acc)
        # вложенные члены структуры (Struct) лежат прямо в Member; в экземплярных DB ещё и в Sections
        mem.children = _parse_members(m, section)
        sec = _child(m, "Sections")
        if sec is not None:
            for s in _children(sec, "Section"):
                mem.children.update(_parse_members(s, section))
        out[mem.name] = mem
    return out


def parse_interface(root) -> dict[str, Member]:
    """Все члены интерфейса блока/типа (секции Input, Output, InOut, Static, ...) одним словарём."""
    out: dict[str, Member] = {}
    for iface in _iter(root, "Interface"):
        for secs in _children(iface, "Sections"):
            for s in _children(secs, "Section"):
                out.update(_parse_members(s, s.get("Name", "")))
        break
    return out


_ARRAY_RE = re.compile(r"^\s*Array\s*\[(.+?)\]\s*of\s+(.+)$", re.I)


def array_bounds(datatype: str):
    """'Array[1..16] of Int' → ([(1, 16)], 'Int'); не массив → (None, datatype)."""
    m = _ARRAY_RE.match(datatype or "")
    if not m:
        return None, datatype
    bounds = []
    for part in m.group(1).split(","):
        lo, _, hi = part.partition("..")
        try:
            bounds.append((int(lo.strip()), int(hi.strip())))
        except ValueError:
            bounds.append((None, None))  # границы через константу: пересчитать нельзя
    return bounds, m.group(2).strip()


# ---------------------------------------------------------------- разбор операндов

@dataclass
class Part:
    name: str
    index: list = field(default_factory=list)   # индексы как в TIA (строки, если не число)


def split_operand(text: str) -> tuple[bool, list[Part]]:
    """'#Fault."A b"[3].x' → (True, [Part('Fault'), Part('A b', [3]), Part('x')]); глобальный '"DB".a' → (False, ...).

    Возвращает (локальный ли операнд, части пути). Кавычки снимаются, точки внутри кавычек не делят путь."""
    s = (text or "").strip()
    local = s.startswith("#")
    if local:
        s = s[1:]
    parts: list[Part] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == ".":
            i += 1
            continue
        if s[i] == '"':
            j = s.index('"', i + 1)
            name, i = s[i + 1:j], j + 1
        else:
            j = i
            while j < n and s[j] not in ".[":
                j += 1
            name, i = s[i:j], j
        idx = []
        while i < n and s[i] == "[":
            j = s.index("]", i)
            for x in s[i + 1:j].split(","):
                x = x.strip()
                idx.append(int(x) if re.fullmatch(r"-?\d+", x) else x)
            i = j + 1
        parts.append(Part(name, idx))
    return local, parts


def is_constant(text: str) -> bool:
    """Пусто, TRUE/FALSE, число, время T#..., строка в кавычках '...' — это не сигнал."""
    s = (text or "").strip()
    if not s:
        return True
    if s.upper() in ("TRUE", "FALSE") or re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
        return True
    return bool(re.match(r"^(T|TIME|LT|S5T|DT|D|TOD|W|DW|B|16|2|8)#", s, re.I)) or s.startswith("'")


def delay_ms(text: str):
    """'T#500ms' / 'T#1s_200ms' / 'T#2m' → миллисекунды; не константа → None."""
    s = (text or "").strip()
    m = re.match(r"^(?:T|TIME|LT)#(.+)$", s, re.I)
    if not m:
        return None
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)", m.group(1).replace("_", ""), re.I):
        found = True
        total += float(num) * {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[unit.lower()]
    return int(total) if found else None


def node_id(path: list[Part]) -> str:
    """Части абсолютного пути (уже с индексами OPC UA) → ns=3;s="DB"."a"[0]."b"."""
    out = []
    for p in path:
        seg = f'"{p.name}"'
        if p.index:
            seg += "[" + ",".join(str(x) for x in p.index) + "]"
        out.append(seg)
    return "ns=3;s=" + ".".join(out)


class Types:
    """Справочник типов для пересчёта индексов массивов: члены FB, UDT и глобальных DB по имени."""

    def __init__(self, by_name: dict[str, dict[str, Member]] | None = None):
        self.by_name = by_name or {}

    def members_of(self, datatype: str) -> dict[str, Member] | None:
        return self.by_name.get((datatype or "").strip().strip('"'))

    def walk(self, members: dict[str, Member] | None, parts: list[Part]):
        """Пересчитывает индексы частей пути с нуля по объявлениям массивов.

        Возвращает (части с индексами OPC UA, последний Member или None, список замечаний)."""
        out: list[Part] = []
        notes: list[str] = []
        last = None
        cur = members
        for p in parts:
            mem = cur.get(p.name) if cur is not None else None
            if cur is not None and mem is None:
                notes.append(f"член «{p.name}» не найден в интерфейсе")
            last = mem
            idx = list(p.index)
            dtype = mem.datatype if mem else ""
            if idx:
                bounds, elem = array_bounds(dtype)
                if bounds and len(bounds) == len(idx) and all(b[0] is not None for b in bounds):
                    idx = [i - b[0] if isinstance(i, int) else i for i, b in zip(idx, bounds)]
                else:
                    notes.append(f"границы массива «{p.name}» неизвестны: индекс оставлен как в TIA")
                dtype = elem if bounds else dtype
            if any(not isinstance(i, int) for i in idx):
                notes.append(f"индекс «{p.name}» не число: адрес переменный")
            out.append(Part(p.name, idx))
            if mem is None:
                cur = None
            elif mem.children:
                cur = mem.children
            else:
                cur = self.members_of(dtype)
        return out, last, notes


# ---------------------------------------------------------------- блоки

@dataclass
class Supervision:
    number: int
    type: str
    operand: str
    status: bool
    delay: str
    conditions: list            # [(операнд, значение срабатывания)]
    category: int
    sub1: int
    sub2: int
    text: dict                  # тексты SpecificField по языкам
    values: dict                # номер сопутствующего значения → операнд


@dataclass
class Call:
    callee: str
    scope: str                  # GlobalVariable / LocalVariable
    instance: list              # части имени экземпляра
    params: dict                # имя параметра → ('global'|'local'|'const', строка или части)


@dataclass
class Block:
    kind: str                   # FB, FC, OB, InstanceDB, GlobalDB
    name: str
    instance_of: str = ""
    members: dict = field(default_factory=dict)
    supervisions: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    path: str = ""


def _bool(text) -> bool:
    return (text or "").strip().lower() == "true"


def _int(text) -> int:
    try:
        return int((text or "0").strip())
    except ValueError:
        return 0


def parse_supervisions(root) -> list[Supervision]:
    out = []
    for sv in _iter(root, "BlockTypeSupervision"):
        conds = []
        for c in _iter(sv, "Condition"):
            op = _child(c, "ConditionOperand")
            conds.append(((op.get("Name") if op is not None else "") or "", _bool(getattr(_child(c, "TriggeringStatus"), "text", ""))))
        values = {}
        for av in _iter(sv, "AssociatedValueOperand"):
            values[_int(av.get("Number"))] = av.get("Name", "")
        sop = _child(sv, "SupervisedOperand")
        dop = _child(sv, "DelayOperand")
        sf = _child(sv, "SpecificField")
        out.append(Supervision(
            number=_int(sv.get("Number")),
            type=sv.get("Type", ""),
            operand=sop.get("Name", "") if sop is not None else "",
            status=_bool(getattr(_child(sv, "SupervisedStatus"), "text", "")),
            delay=dop.get("Name", "") if dop is not None else "",
            conditions=conds,
            category=_int(getattr(_child(sv, "CategoryNumber"), "text", "0")),
            sub1=_int(getattr(_child(sv, "SubCategory1Number"), "text", "0")),
            sub2=_int(getattr(_child(sv, "SubCategory2Number"), "text", "0")),
            text=_texts(_child(sf, "SpecificFieldText")) if sf is not None else {},
            values=values,
        ))
    return out


def _access_value(acc):
    """Элемент <Access> → ('global'|'local', [Part]) или ('const', текст) или None."""
    scope = acc.get("Scope", "")
    if scope in ("LiteralConstant", "TypedConstant", "GlobalConstant", "LocalConstant"):
        cv = next(_iter(acc, "ConstantValue"), None)
        return ("const", (cv.text or "") if cv is not None else "")
    sym = _child(acc, "Symbol")
    if sym is None or scope not in ("GlobalVariable", "LocalVariable"):
        return None
    parts = []
    for comp in _children(sym, "Component"):
        idx = []
        for sub in _children(comp, "Access"):
            v = _access_value(sub)
            idx.append(int(v[1]) if v and v[0] == "const" and re.fullmatch(r"-?\d+", v[1] or "") else "?")
        parts.append(Part(comp.get("Name", ""), idx))
    return ("global" if scope == "GlobalVariable" else "local", parts)


def _parse_call(call_info, actuals: dict) -> Call | None:
    inst = _child(call_info, "Instance")
    if inst is None:
        return None
    comps = [Part(c.get("Name", "")) for c in _children(inst, "Component")]
    params = dict(actuals)
    # SCL: фактический параметр лежит внутри <Parameter>
    for p in _children(call_info, "Parameter"):
        acc = next(_iter(p, "Access"), None)
        if acc is not None and p.get("Name") not in params:
            v = _access_value(acc)
            if v:
                params[p.get("Name")] = v
    return Call(call_info.get("Name", ""), inst.get("Scope", ""), comps, params)


def parse_calls(root) -> list[Call]:
    """Вызовы FB с экземпляром и их фактические параметры (LAD/FBD по проводам, SCL по <Parameter>)."""
    out: list[Call] = []
    seen = set()
    for net in _iter(root, "FlgNet"):
        parts = _child(net, "Parts")
        wires = _child(net, "Wires")
        access = {}
        calls = {}
        if parts is not None:
            for p in parts:
                if _local(p.tag) == "Access":
                    access[p.get("UId")] = p
                elif _local(p.tag) == "Call":
                    calls[p.get("UId")] = p
        actual: dict[str, dict] = {uid: {} for uid in calls}
        if wires is not None:
            for w in _children(wires, "Wire"):
                idents = [c.get("UId") for c in _children(w, "IdentCon")]
                for nc in _children(w, "NameCon"):
                    if nc.get("UId") in calls:
                        for uid in idents:
                            if uid in access:
                                v = _access_value(access[uid])
                                if v:
                                    actual[nc.get("UId")][nc.get("Name")] = v
        for uid, c in calls.items():
            ci = _child(c, "CallInfo")
            if ci is not None:
                call = _parse_call(ci, actual[uid])
                if call:
                    out.append(call)
                    seen.add(id(ci))
    for ci in _iter(root, "CallInfo"):   # вызовы в SCL/STL (вне FlgNet)
        if id(ci) not in seen:
            call = _parse_call(ci, {})
            if call:
                out.append(call)
    return out


_BLOCK_KINDS = {"SW.Blocks.FB": "FB", "SW.Blocks.FC": "FC", "SW.Blocks.OB": "OB",
                "SW.Blocks.InstanceDB": "InstanceDB", "SW.Blocks.GlobalDB": "GlobalDB",
                "SW.Types.PlcStruct": "UDT"}


def parse_block(xml: bytes | str, path: str = "") -> Block | None:
    """Один XML-файл выгрузки → Block (тип, имя, интерфейс, контроли, вызовы)."""
    root = ET.fromstring(xml)
    for el in root:
        kind = _BLOCK_KINDS.get(_local(el.tag))
        if not kind:
            continue
        attrs = _child(el, "AttributeList")
        name = getattr(_child(attrs, "Name"), "text", "") if attrs is not None else ""
        blk = Block(kind, name or "", path=path)
        if attrs is not None:
            blk.instance_of = getattr(_child(attrs, "InstanceOfName"), "text", "") or ""
        blk.members = parse_interface(el)
        if kind == "FB":
            blk.supervisions = parse_supervisions(el)
        if kind in ("FB", "FC", "OB"):
            blk.calls = parse_calls(el)
        return blk
    return None


def load_blocks(folder: Path) -> list[Block]:
    out = []
    for p in sorted(Path(folder).rglob("*.xml")):
        try:
            b = parse_block(p.read_bytes(), str(p.relative_to(folder)))
        except ET.ParseError:
            continue
        if b:
            out.append(b)
    return out


# ---------------------------------------------------------------- экземпляры

@dataclass
class Instance:
    path: list                  # части абсолютного пути экземпляра (первая — имя DB)
    caller: str = ""            # блок, где вызван
    params: dict = field(default_factory=dict)
    parent: "Instance | None" = None
    note: str = ""


def instance_paths(blocks: list[Block]) -> dict[str, list[Instance]]:
    """Все абсолютные пути экземпляров каждого FB: экземплярные DB и мультиэкземпляры по всему дереву вызовов."""
    by_name = {b.name: b for b in blocks}
    calls_to: dict[str, list[tuple[Block, Call]]] = {}
    for b in blocks:
        for c in b.calls:
            calls_to.setdefault(c.callee, []).append((b, c))
    memo: dict[str, list[Instance]] = {}

    def paths(fb: str, stack: tuple = ()) -> list[Instance]:
        if fb in memo:
            return memo[fb]
        if fb in stack:
            return []
        out: list[Instance] = []
        keys = set()
        for caller, c in calls_to.get(fb, []):
            if c.scope == "GlobalVariable":
                cand = [Instance(list(c.instance), caller.name, c.params)]
            elif caller.kind == "FB":
                cand = [Instance(par.path + list(c.instance), caller.name, c.params, par)
                        for par in paths(caller.name, stack + (fb,))]
            else:
                cand = [Instance(list(c.instance), caller.name, c.params,
                                 note=f"мультиэкземпляр в {caller.kind} {caller.name}: путь не определить")]
            for inst in cand:
                k = tuple(p.name for p in inst.path)
                if k not in keys:
                    keys.add(k)
                    out.append(inst)
        for b in blocks:   # экземплярные DB, которых нет среди вызовов
            if b.kind == "InstanceDB" and b.instance_of == fb and (b.name,) not in keys:
                keys.add((b.name,))
                out.append(Instance([Part(b.name)], note="вызов этого экземплярного DB не найден в выгрузке"))
        memo[fb] = out
        return out

    return {b.name: paths(b.name) for b in blocks if b.kind == "FB" and b.supervisions and b.name in by_name}


# ---------------------------------------------------------------- справочник

@dataclass
class Record:
    plc: str
    source: str
    instance: str
    number: int
    sv_type: str
    signal: str
    node: str
    status: bool
    delay_ms: object
    conditions: list            # [(адрес или имя, значение)]
    category: int
    sub1: int
    sub2: int
    text: str
    actual: str = ""            # адрес фактического параметра вызова (если сигнал — вход/выход FB)
    note: str = ""

    @property
    def read_node(self) -> str:
        """Что читать по OPC UA: сам сигнал, а если он закрыт (ExternalAccessible=false) — подключённый глобальный сигнал."""
        if self.node and "ExternalAccessible" not in self.note and "экземпляров нет" not in self.note:
            return self.node
        return self.actual if self.actual.startswith("ns=") else ""


def _types_of(blocks: list[Block]) -> Types:
    t = Types()
    for b in blocks:
        if b.kind in ("FB", "UDT", "GlobalDB"):
            t.by_name[b.name] = b.members
    for b in blocks:   # экземплярный DB знает члены своего FB
        if b.kind == "InstanceDB" and b.instance_of in t.by_name:
            t.by_name.setdefault(b.name, t.by_name[b.instance_of])
    return t


def resolve_global(text: str, types: Types):
    """'"DB".a[2]' → (node_id, Member, замечания); первая часть — DB или тег ПЛК."""
    _, parts = split_operand(text)
    if not parts:
        return "", None, ["пустой операнд"]
    head = parts[0]
    members = types.by_name.get(head.name)
    rest, last, notes = types.walk(members, parts[1:]) if members is not None else (parts[1:], None, [])
    if members is None and len(parts) > 1:
        notes = [f"блок «{head.name}» не найден в выгрузке: индексы как в TIA"]
    return node_id([Part(head.name, head.index)] + rest), last, notes


def resolve_local(text: str, inst: Instance, fb_members: dict, types: Types):
    """'#X.Y' экземпляра → (node_id, Member, замечания, доступен ли из OPC UA)."""
    _, parts = split_operand(text)
    rest, last, notes = types.walk(fb_members, parts)
    accessible = True
    mem = fb_members.get(parts[0].name) if parts else None
    if mem is not None and not mem.accessible:
        accessible = False
    return node_id(inst.path + rest), last, notes, accessible


def _path_accessible(inst: Instance, types: Types) -> bool:
    """Все мультиэкземпляры по пути открыты для OPC UA (ExternalAccessible)."""
    members = types.by_name.get(inst.path[0].name) if inst.path else None
    for p in inst.path[1:]:
        mem = (members or {}).get(p.name)
        if mem is None:
            return True   # неизвестно: не утверждаем, что закрыто
        if not mem.accessible:
            return False
        members = mem.children or types.members_of(mem.datatype)
    return True


def _actual_node(kind_val, inst: Instance, types: Types, fb_lookup) -> str:
    """Фактический параметр вызова → адрес OPC UA (глобальный или локальный член вызывающего FB)."""
    kind, val = kind_val
    if kind == "global":
        nid, _, _ = resolve_global(".".join(f'"{p.name}"' + (f"[{','.join(map(str, p.index))}]" if p.index else "")
                                            for p in val), types)
        return nid
    if kind == "local" and inst.parent is not None:
        caller = fb_lookup.get(inst.caller)
        if caller is not None:
            rest, _, _ = types.walk(caller.members, val)
            return node_id(inst.parent.path + rest)
    if kind == "local":
        return "#" + ".".join(p.name for p in val) + f" (локальная переменная {inst.caller})"
    return ""


def const_text(value: str) -> str:
    """Константа вызова как текст: "WSTRING#'RB104 check car'" → 'RB104 check car'."""
    v = re.sub(r"^[A-Za-z_]+#", "", (value or "").strip())
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == "'" else v


def _fill_text(fmt: str, values: dict, consts: dict) -> str:
    """'@5%s@: @6%t#List@' → подстановка констант вызова (@4 = 1-е сопутствующее значение)."""
    def rep(m):
        num = int(m.group(1))
        spec = m.group(2)
        op = values.get(num - 3, "")
        name = op.lstrip("#").strip('"')
        if spec.startswith("t#"):
            return f"[текст из списка {spec[2:]}]"
        return consts.get(name, f"<{name}>" if name else "")
    return re.sub(r"@(\d+)%([^@]*)@", rep, fmt or "")


def build_catalog(blocks: list[Block], general_rows: list[dict] | None = None, plc: str = PLC_NAME) -> list[Record]:
    """Все контроли: экземпляры FB-типов (по дереву вызовов) и глобальные General_PD."""
    types = _types_of(blocks)
    fbs = {b.name: b for b in blocks if b.kind == "FB"}
    out: list[Record] = []
    for fb_name, insts in instance_paths(blocks).items():
        fb = fbs[fb_name]
        if not insts:
            for sv in fb.supervisions:
                out.append(Record(plc, fb_name, "", sv.number, sv.type, sv.operand, "", sv.status, delay_ms(sv.delay), [],
                                  sv.category, sv.sub1, sv.sub2, "", note="FB нигде не вызван: экземпляров нет"))
            continue
        for inst in insts:
            inst_name = ".".join(p.name for p in inst.path)
            path_ok = _path_accessible(inst, types)
            consts = {k: const_text(v[1]) for k, v in inst.params.items() if v[0] == "const"}
            for sv in fb.supervisions:
                notes = [inst.note] if inst.note else []
                nid, mem, n2, acc = resolve_local(sv.operand, inst, fb.members, types)
                notes += n2
                if not (acc and path_ok):
                    notes.append("член закрыт для OPC UA (ExternalAccessible=false)")
                if mem is not None and mem.datatype.lower() != "bool":
                    notes.append(f"тип сигнала {mem.datatype}, не Bool")
                actual = ""
                pname = split_operand(sv.operand)[1][0].name if sv.operand else ""
                if mem is not None and mem.section in ("Input", "Output", "InOut") and pname in inst.params:
                    actual = _actual_node(inst.params[pname], inst, types, fbs)
                    if not actual and inst.params[pname][0] == "const":
                        actual = "константа " + inst.params[pname][1]
                conds = []
                for cop, cst in sv.conditions:
                    if is_constant(cop):
                        continue
                    if cop.startswith("#"):
                        cn, _, cnotes, cacc = resolve_local(cop, inst, fb.members, types)
                        notes += [f"условие: {x}" for x in cnotes]
                        if not cacc:
                            notes.append(f"условие {cop} закрыто для OPC UA")
                    else:
                        cn, _, cnotes = resolve_global(cop, types)
                        notes += [f"условие: {x}" for x in cnotes]
                    conds.append((cn, cst))
                d = delay_ms(sv.delay)
                if d is None and sv.delay and not is_constant(sv.delay):
                    dn = resolve_local(sv.delay, inst, fb.members, types)[0] if sv.delay.startswith("#") else resolve_global(sv.delay, types)[0]
                    notes.append(f"задержка из переменной {dn}")
                comment = pick_text(mem.comment) if mem else ""
                spec = _fill_text(pick_text(sv.text), sv.values, consts)
                text = " · ".join(x for x in (comment, spec) if x)
                out.append(Record(plc, fb_name, inst_name, sv.number, sv.type, sv.operand, nid, sv.status, d, conds,
                                  sv.category, sv.sub1, sv.sub2, text, actual, "; ".join(dict.fromkeys(notes))))
    for row in general_rows or []:
        out.append(_general_record(row, types, plc))
    return out


def _general_record(row: dict, types: Types, plc: str) -> Record:
    """Строка листа «ProDiag Supervisions» (глобальный контроль General_PD) → Record."""
    notes = []
    tag = row.get("tag", "")
    nid, mem, n2 = resolve_global(tag, types)
    notes += n2
    if mem is not None and not mem.accessible:
        notes.append("член закрыт для OPC UA (ExternalAccessible=false)")
    conds = []
    for cop, cst in row.get("conditions", []):
        if is_constant(cop):
            continue
        cn, _, cnotes = resolve_global(cop, types) if not cop.startswith("#") else (cop, None, ["локальный операнд в глобальном контроле"])
        notes += [f"условие: {x}" for x in cnotes]
        conds.append((cn, cst))
    comment = pick_text(mem.comment) if mem else ""
    text = " · ".join(x for x in (comment, row.get("text", "")) if x)
    return Record(plc, row.get("fb", GENERAL_SOURCE), "", row.get("id", 0), row.get("type", ""), tag, nid,
                  row.get("status", True), delay_ms(row.get("delay", "")), conds, row.get("category", 0),
                  row.get("sub1", 0), row.get("sub2", 0), text, "", "; ".join(dict.fromkeys(notes)))


# ---------------------------------------------------------------- чтение xlsx без openpyxl

_XNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_xlsx(path) -> dict[str, list[dict[str, str]]]:
    """Все листы xlsx → {лист: [{буква столбца: значение}]} (zipfile + xml, без сторонних библиотек)."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_XNS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_XNS}t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find(f"{_XNS}sheets"):
        target = rels[s.get(f"{_RNS}id")].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for r in ET.fromstring(z.read(target)).iter(f"{_XNS}row"):
            row = {}
            for c in r.findall(f"{_XNS}c"):
                col = re.match(r"[A-Z]+", c.get("r")).group(0)
                v = c.find(f"{_XNS}v")
                if c.get("t") == "s":
                    val = shared[int(v.text)]
                elif c.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{_XNS}t"))
                else:
                    val = v.text if v is not None and v.text else ""
                row[col] = val
            rows.append(row)
        out[s.get("name")] = rows
    return out


def general_rows_from_xlsx(rows: list[dict]) -> list[dict]:
    """Лист «ProDiag Supervisions» → записи глобальных контролей (заголовок узнаётся по первой ячейке)."""
    out = []
    head = None
    for r in rows:
        if r.get("A") == "ProDiag FB":
            head = {v: k for k, v in r.items()}
            continue
        if head is None or not r.get("A"):
            continue
        g = lambda title: r.get(head.get(title, ""), "")   # noqa: E731
        txt = next((r.get(k, "") for t, k in head.items() if "[en-US]" in t and r.get(k)), "") or \
            next((r.get(k, "") for t, k in head.items() if "[ru-RU]" in t and r.get(k)), "")
        out.append({
            "fb": g("ProDiag FB").strip('"'),
            "id": _int(g("ID")),
            "tag": g("Supervised tag"),
            "status": _bool(g("Trigger")),
            "type": {"O": "Operand"}.get(g("Type of supervision"), g("Type of supervision")),
            "category": _int(g("Category")), "sub1": _int(g("Subcategory 1")), "sub2": _int(g("Subcategory 2")),
            "delay": g("Delay time"),
            "conditions": [(g(f"Condition {i}"), _bool(g(f"C{i} trigger"))) for i in (1, 2, 3)],
            "text": txt,
        })
    return out


def category_texts(project_texts_rows: list[dict]) -> list[str]:
    """Описания категорий ProDiag из ProjectTexts (<ProDiagTextCategory>, en-US в столбце E), по порядку в файле."""
    return [r.get("E", "") for r in project_texts_rows if r.get("A") == "<ProDiagTextCategory>" and r.get("E")]


# ---------------------------------------------------------------- Excel

SV_TYPES = {"Operand": "Операнд", "MessageError": "Сообщение: ошибка", "MessageWarning": "Сообщение: предупреждение",
            "MessageText": "Сообщение: информация"}


def write_xlsx(records: list[Record], out_path, cat_texts: list[str] | None = None) -> None:
    import xlsxwriter

    cat_texts = cat_texts or []
    wb = xlsxwriter.Workbook(str(out_path))
    head = wb.add_format({"bold": True, "text_wrap": True, "valign": "top", "bg_color": "#D9E1F2", "border": 1})
    wrap = wb.add_format({"text_wrap": False, "valign": "top"})
    num = wb.add_format({"num_format": "0", "valign": "top"})
    red = wb.add_format({"font_color": "#C00000", "valign": "top"})

    ws = wb.add_worksheet("Аварии")
    cols = [("№", 6, "Порядковый номер строки"),
            ("ПЛК", 7, "ПЛК, из выгрузки которого взят контроль"),
            ("Источник", 22, "Тип FB с контролями ProDiag или General_PD (глобальные контроли)"),
            ("Экземпляр", 40, "Абсолютный путь экземпляра FB: экземплярный DB и мультиэкземпляры через точку"),
            ("№ контроля", 8, "Номер контроля внутри типа FB (или ID для General_PD)"),
            ("Тип контроля", 16, "Операнд или сообщение (ошибка/информация) по выгрузке TIA"),
            ("Сигнал", 32, "Контролируемый операнд, как записан в TIA"),
            ("Адрес OPC UA", 60, "Абсолютный узел OPC UA сигнала в экземпляре (индексы массивов с нуля)"),
            ("Фактический параметр", 55, "Что подключено к этому входу/выходу FB в месте вызова (глобальный адрес OPC UA)"),
            ("Читать (OPC UA)", 60, "Адрес для чтения: сигнал, а если он закрыт для OPC UA — подключённый к нему глобальный сигнал; пусто — читать нечего"),
            ("Авария при", 9, "Значение сигнала, при котором срабатывает авария (TRUE/FALSE)"),
            ("Задержка, мс", 9, "Задержка срабатывания, мс"),
            ("Условие 1", 50, "Условие 1: адрес = значение, при котором контроль активен"),
            ("Условие 2", 30, "Условие 2"),
            ("Условие 3", 30, "Условие 3"),
            ("Категория", 9, "Номер категории ProDiag"),
            ("Подкатегории", 10, "Подкатегория 1 / подкатегория 2"),
            ("Текст", 60, "Комментарий операнда в интерфейсе FB и текст контроля с подставленными константами вызова"),
            ("Примечание", 60, "Что не удалось разрешить или на что обратить внимание")]
    for i, (title, w, _tip) in enumerate(cols):
        ws.set_column(i, i, w)
    data = []
    for n, r in enumerate(records, 1):
        cond = [f"{a} = {'TRUE' if v else 'FALSE'}" for a, v in r.conditions] + ["", "", ""]
        data.append([n, r.plc, r.source, r.instance, r.number, SV_TYPES.get(r.sv_type, r.sv_type), r.signal, r.node,
                     r.actual, r.read_node, "TRUE" if r.status else "FALSE", r.delay_ms if r.delay_ms is not None else "",
                     cond[0], cond[1], cond[2], r.category, f"{r.sub1}/{r.sub2}", r.text, r.note])
    ws.add_table(0, 0, max(len(data), 1), len(cols) - 1, {
        "data": data or [[""] * len(cols)], "name": "Аварии_ProDiag", "style": "Table Style Light 9",
        "columns": [{"header": c[0], "header_format": head} for c in cols]})
    for i, (_t, _w, tip) in enumerate(cols):
        ws.write_comment(0, i, tip, {"x_scale": 2})
    for row_i, r in enumerate(records, 1):
        if r.note:
            ws.write(row_i, len(cols) - 1, r.note, red)
    ws.set_row(0, 30)
    ws.freeze_panes(1, 0)
    _ = (wrap, num)

    # ---- Итог
    s = wb.add_worksheet("Итог")
    bold = wb.add_format({"bold": True})
    s.set_column(0, 0, 60)
    s.set_column(1, 3, 14)
    row = 0
    s.write(row, 0, "Всего контролей (строк на листе «Аварии»)", bold)
    s.write(row, 1, len(records))
    row += 1
    noted = sum(1 for r in records if r.note)
    s.write(row, 0, "Из них с примечанием", bold)
    s.write(row, 1, noted)
    row += 1
    closed = sum(1 for r in records if "ExternalAccessible" in r.note)
    s.write(row, 0, "Сигнал закрыт для OPC UA (читать фактический параметр)", bold)
    s.write(row, 1, closed)
    row += 1
    with_actual = sum(1 for r in records if r.actual.startswith("ns="))
    s.write(row, 0, "Есть глобальный фактический параметр", bold)
    s.write(row, 1, with_actual)
    row += 1
    s.write(row, 0, "Есть адрес для чтения (столбец «Читать (OPC UA)»)", bold)
    s.write(row, 1, sum(1 for r in records if r.read_node))
    row += 2
    s.write_row(row, 0, ["Источник", "Контролей в типе", "Экземпляров", "Строк"], head)
    row += 1
    by_src: dict[str, list[Record]] = {}
    for r in records:
        by_src.setdefault(r.source, []).append(r)
    for src in sorted(by_src):
        rs = by_src[src]
        nums = {r.number for r in rs}
        insts = {r.instance for r in rs if r.instance}
        s.write_row(row, 0, [src, len(nums), len(insts), len(rs)])
        row += 1
    row += 1
    s.write_row(row, 0, ["Примечание (почему не разрешено)", "Строк"], head)
    row += 1
    reasons: dict[str, int] = {}
    for r in records:
        for part in filter(None, (x.strip() for x in r.note.split(";"))):
            key = re.sub(r"«[^»]*»|ns=3;s=\S+|#\S+", "…", part)
            reasons[key] = reasons.get(key, 0) + 1
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        s.write_row(row, 0, [k, v])
        row += 1
    if cat_texts:
        row += 1
        s.write(row, 0, "Описания категорий ProDiag из ProjectTexts (порядок в файле; соответствие номерам не подтверждено)", bold)
        row += 1
        for i, t in enumerate(cat_texts, 1):
            s.write_row(row, 0, [f"{i}. {t}"])
            row += 1
    wb.close()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    folder = Path(argv[1])
    out = Path(argv[2])
    blocks = load_blocks(folder / "Blocks") + load_blocks(folder / "Types") if (folder / "Blocks").exists() else load_blocks(folder)
    general = []
    sup = folder / "ProDiag_Supervisions.xlsx"
    if sup.exists():
        sheets = read_xlsx(sup)
        general = general_rows_from_xlsx(sheets.get("ProDiag Supervisions", []))
    cats = []
    pt = Path(argv[3]) if len(argv) > 3 else folder.parent / "ProjectTexts_ru-RU.xlsx"
    if pt.exists():
        cats = category_texts(next(iter(read_xlsx(pt).values()), []))
    records = build_catalog(blocks, general)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(records, out, cats)
    by = {}
    for r in records:
        by[r.source] = by.get(r.source, 0) + 1
    print(f"контролей: {len(records)}, с примечанием: {sum(1 for r in records if r.note)}, источников: {len(by)}")
    print(f"файл: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
