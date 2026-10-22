"""Live GWM overview. Reads existing discovery metadata and OPC values; never writes.

OPC station arrays are bound by the ordinal of discovered children, not by the
numeric suffix in a constructed NodeId. Siemens exposes these arrays zero based
although the saved PLC declarations start at one.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/scada", tags=["scada"])
POLLER_URL = os.getenv("POLLER_URL", "http://plc-monitoring-docker-poller:8080")
GROUPS = {
    "GWM_UB": tuple([f"UB{i}" for i in range(10, 100, 10)] + [f"MB{i}" for i in range(100, 140, 10)]),
    "GWM_MB": tuple(f"MB{i}" for i in range(140, 280, 10)),
}
SENSORS = {"OutLet": "SI_Outlet", "End": "SI_End", "Slow": "SI_Slow", "Start": "SI_Start", "InLet": "SI_Inlet"}
ERROR_LABELS = {
    "Power": "Питание", "Estop": "Аварийный останов", "Bus": "Шина связи",
    "Laser": "Световая завеса", "Gate": "Защитная дверь", "WaterAir": "Вода / воздух",
    "Sensor": "Датчик", "OverTime": "Превышение времени", "Motor": "Двигатель",
    "Driver": "Привод", "TypeErr": "Тип кузова", "Part": "Заготовка", "Robot": "Робот",
    "TimerE": "Контроллер сварки", "RFID": "RFID", "BarCode": "Сканер штрихкода",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _blank_details() -> dict[str, Any]:
    return {"carid": None, "carid_status": "unavailable", "skid_present": None, "causes": [], "alarms": [],
            "diagnostics_complete": False, "alarm_mapping_complete": False,
            "cause_verified": False, "details_loaded": False, "details_error": ""}


def blank_snapshot() -> dict[str, Any]:
    return {
        "stations": {sid: {"color": None, "RUN_FWD": None, "RUN_BWD": None,
                            "sensors": dict.fromkeys(SENSORS), "plc": {"id": None, "name": name, "ip": ""},
                            "binding": {"color_index": i}, **_blank_details()}
                     for name, stations in GROUPS.items() for i, sid in enumerate(stations, 1)},
        "plcs": [{"name": name, "ok": False, "error": "Нет данных"} for name in GROUPS],
        "updated_at": _now(),
    }


def bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def good_value(values: dict, node: str | None) -> Any:
    item = values.get(node, {})
    return item.get("v") if isinstance(item, dict) and item.get("ok") is True else None


def indexed_children(children: list[dict]) -> list[dict]:
    """Only numeric array elements, in numeric order. Duplicate indices fail closed."""
    items = []
    for child in children:
        name = str(child.get("variable_name", ""))
        if re.fullmatch(r"\[?\d+\]?", name):
            items.append((int(name.strip("[]")), child))
    items.sort(key=lambda item: item[0])
    if len({i for i, _ in items}) != len(items):
        return []
    # Missing elements must not silently shift all subsequent station bindings.
    # These GWM OPC arrays have an observed zero lower bound. A missing first
    # child must not make child 1 look like station 1 (which is actually child 0).
    if items and [i for i, _ in items] != list(range(len(items))):
        return []
    return [child for _, child in items]


class GwmService:
    def __init__(self, pool: Any):
        self.pool = pool
        self.lock = asyncio.Lock()
        self.children: dict[tuple[int, str], list[dict]] = {}
        self.metadata_at = 0.0
        self.plcs: dict[str, dict] = {}
        self.roots: dict[tuple[int, str, str], str] = {}
        self.last: dict | None = None
        self.last_station: str | None = None
        self.last_at = 0.0

    async def _metadata(self) -> None:
        if time.monotonic() - self.metadata_at < 30:
            return
        rows = await self.pool.fetch(
            "SELECT id, name, opc_endpoint, is_active FROM plcs WHERE name = ANY($1::text[]) ORDER BY id",
            list(GROUPS),
        )
        plcs: dict[str, dict] = {}
        # Ambiguous PLC names are deliberately not bound.
        for name in GROUPS:
            matches = [dict(row) for row in rows if row["name"] == name]
            if len(matches) == 1:
                plcs[name] = matches[0]
        roots: dict[tuple[int, str, str], str] = {}
        if plcs:
            nodes = await self.pool.fetch(
                "SELECT plc_id, db_name, variable_name, node_id FROM plc_discovered_nodes "
                "WHERE plc_id = ANY($1::int[]) AND db_name = ANY($2::text[])",
                [p["id"] for p in plcs.values()], ["user", "Stn_Trans", "Stn", "Cell", "Car_Stn", "BODY_SN", "MSG"],
            )
            for row in nodes:
                # A root must be the direct symbol DB.member, not a nested duplicate.
                db, name, node = row["db_name"], row["variable_name"], row["node_id"]
                if not re.search(r';s="' + re.escape(db) + r'"\."' + re.escape(name) + r'"$', node):
                    continue
                key = (row["plc_id"], db, name)
                if key in roots and roots[key] != node:
                    roots[key] = ""
                else:
                    roots[key] = node
        if roots != self.roots or plcs != self.plcs:
            self.children.clear()
        self.plcs, self.roots = plcs, roots
        self.metadata_at = time.monotonic()

    async def _children(self, client: httpx.AsyncClient, plc_id: int, parent: str | None) -> list[dict]:
        if not parent:
            return []
        key = (plc_id, parent)
        if key not in self.children:
            response = await client.get(f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/children",
                                        params={"node_id": parent, "shallow": "true"})
            response.raise_for_status()
            children = response.json()
            if not isinstance(children, list):
                raise ValueError("Некорректный ответ списка сигналов")
            self.children[key] = children
        return self.children[key]

    async def _field(self, client: httpx.AsyncClient, plc_id: int, parent: str | None, name: str) -> str | None:
        matches = [row["node_id"] for row in await self._children(client, plc_id, parent)
                   if row.get("variable_name") == name and row.get("node_id")]
        return matches[0] if len(matches) == 1 else None

    async def _element(self, client: httpx.AsyncClient, plc_id: int, root: str | None, ordinal: int) -> str | None:
        items = indexed_children(await self._children(client, plc_id, root))
        return items[ordinal]["node_id"] if 0 <= ordinal < len(items) else None

    async def _numeric_element(self, client: httpx.AsyncClient, plc_id: int, root: str | None, index: int) -> str | None:
        # CELL and MSG.W declarations start at zero. They use the logical index
        # directly, unlike STN/STN_C and VIN, which use discovered ordinals.
        matches = [row["node_id"] for row in await self._children(client, plc_id, root)
                   if str(row.get("variable_name", "")).strip("[]") == str(index) and row.get("node_id")]
        return matches[0] if len(matches) == 1 else None

    async def _values(self, client: httpx.AsyncClient, plc_id: int, nodes: list[str | None]) -> dict:
        unique = list(dict.fromkeys(n for n in nodes if n))
        values: dict = {}
        for start in range(0, len(unique), 200):
            response = await client.post(f"{POLLER_URL}/internal/plcs/{plc_id}/opcua/values",
                                         json={"node_ids": unique[start:start + 200]})
            response.raise_for_status()
            batch = response.json().get("values", {})
            if not isinstance(batch, dict):
                raise ValueError("Некорректный ответ значений ПЛК")
            values.update(batch)
        return values

    async def _basic(self, client: httpx.AsyncClient, name: str, output: dict) -> None:
        status = next(item for item in output["plcs"] if item["name"] == name)
        plc = self.plcs.get(name)
        if not plc or not plc["is_active"]:
            status["error"] = "ПЛК не найден или выключен из опроса"
            return
        pid = plc["id"]
        info = {"id": pid, "name": name, "ip": urlsplit(plc["opc_endpoint"] or "").hostname or ""}
        for sid in GROUPS[name]:
            output["stations"][sid]["plc"] = info
        color = self.roots.get((pid, "user", "STN_Screen color"))
        values = await self._values(client, pid, [color])
        colors = good_value(values, color)
        if isinstance(colors, list):
            for i, sid in enumerate(GROUPS[name]):
                value = colors[i] if i < len(colors) else None
                output["stations"][sid]["color"] = value if isinstance(value, int) and not isinstance(value, bool) else None
        bindings: dict[str, dict] = {}
        trans = self.roots.get((pid, "Stn_Trans", "STN"))
        for i, sid in enumerate(GROUPS[name]):
            parent = await self._element(client, pid, trans, i)
            fields = await self._children(client, pid, parent)
            by_name = {r.get("variable_name"): r.get("node_id") for r in fields}
            bindings[sid] = {field: by_name.get(field) for field in ["RUN_FWD", "RUN_BWD", *SENSORS.values()]}
        values = await self._values(client, pid, [node for fields in bindings.values() for node in fields.values()])
        complete = True
        for sid, fields in bindings.items():
            station = output["stations"][sid]
            for signal in ("RUN_FWD", "RUN_BWD"):
                station[signal] = bool_value(good_value(values, fields[signal]))
            station["sensors"] = {label: bool_value(good_value(values, fields[field])) for label, field in SENSORS.items()}
            complete &= station["color"] is not None and all(v is not None for v in station["sensors"].values())
            complete &= station["RUN_FWD"] is not None and station["RUN_BWD"] is not None
        status.update(ok=bool(complete), error="" if complete else "Часть сигналов недоступна")

    async def _details(self, client: httpx.AsyncClient, sid: str, output: dict) -> None:
        station = output["stations"][sid]
        name = station["plc"]["name"]
        plc = self.plcs.get(name)
        if not plc or not plc["is_active"]:
            station["details_error"] = "ПЛК недоступен"
            return
        pid, ordinal = plc["id"], GROUPS[name].index(sid)
        # A missing alarm-word mapping must not suppress the independently read VIN.
        try:
            await self._carid(client, pid, ordinal, station)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            station["carid_status"] = "unavailable"
        parent = await self._element(client, pid, self.roots.get((pid, "Stn", "STN")), ordinal)
        ew = await self._field(client, pid, parent, "EW1")
        zw = await self._field(client, pid, parent, "zW")
        cfg = await self._field(client, pid, parent, "CFG")
        err_stop = await self._field(client, pid, zw, "Err_Stop")
        cell_id = await self._field(client, pid, cfg, "Cell")
        flags = {row["variable_name"]: row["node_id"] for row in await self._children(client, pid, ew)
                 if row.get("variable_name") in ERROR_LABELS and row.get("node_id")}
        values = await self._values(client, pid, [err_stop, cell_id, *flags.values()])
        stopped = bool_value(good_value(values, err_stop))
        flag_values = {key: bool_value(good_value(values, flags.get(key))) for key in ERROR_LABELS}
        cell = good_value(values, cell_id)
        cell_error = None
        cell_err_node = None
        if isinstance(cell, int) and not isinstance(cell, bool) and cell >= 0:
            cell_parent = await self._numeric_element(client, pid, self.roots.get((pid, "Cell", "CELL")), cell)
            cell_zw = await self._field(client, pid, cell_parent, "zw")
            cell_err_node = await self._field(client, pid, cell_zw, "ErrH")
            cell_values = await self._values(client, pid, [cell_err_node])
            cell_error = bool_value(good_value(cell_values, cell_err_node))
        verified = name == "GWM_MB"
        station["cause_verified"] = verified
        station["err_stop"] = stopped
        station["causes"] = [{"text": ERROR_LABELS[key], "source": flags[key], "is_cause": verified and stopped is True}
                             for key, value in flag_values.items() if value is True]
        if cell_error is True:
            station["causes"].append({"text": f"Общая ошибка ячейки {cell}", "source": cell_err_node,
                                       "is_cause": verified and stopped is True})
        station["diagnostics_complete"] = stopped is not None and cell_error is not None and all(v is not None for v in flag_values.values())
        if verified:
            try:
                await self._messages(client, pid, ordinal + 1, cell, flag_values, cell_error, stopped, station)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                station["diagnostics_complete"] = False
        station["details_error"] = "" if station["diagnostics_complete"] else "Часть сигналов диагностики недоступна"
        station["details_loaded"] = True

    async def _carid(self, client: httpx.AsyncClient, pid: int, ordinal: int, station: dict) -> None:
        # CARID: current station body pointer, not the retained sent-body history.
        if station.get("plc", {}).get("name") == "GWM_MB" and ordinal == 0:
            # MB140 has no transport block in the saved station program. Its
            # default NoSkid field does not establish real occupancy.
            return
        trans = await self._element(client, pid, self.roots.get((pid, "Stn_Trans", "STN")), ordinal)
        no_skid_node = await self._field(client, pid, trans, "NoSkid")
        no_skid = bool_value(good_value(await self._values(client, pid, [no_skid_node]), no_skid_node))
        station["skid_present"] = None if no_skid is None else not no_skid
        if no_skid is not False:
            # A retained pointer/VIN alone cannot prove a body is currently here.
            station["carid_status"] = "empty" if no_skid is True else "unavailable"
            return
        car_parent = await self._element(client, pid, self.roots.get((pid, "Car_Stn", "STN_C")), ordinal)
        car_type = await self._field(client, pid, car_parent, "CarType")
        pointer_node = await self._field(client, pid, car_type, "P1")
        pointer = good_value(await self._values(client, pid, [pointer_node]), pointer_node)
        if isinstance(pointer, int) and not isinstance(pointer, bool) and 0 <= pointer < 100:
            vin_parent = await self._element(client, pid, self.roots.get((pid, "BODY_SN", "VIN")), pointer)
            code_node = await self._field(client, pid, vin_parent, "Code")
            code = good_value(await self._values(client, pid, [code_node]), code_node)
            if isinstance(code, str):
                station["carid"] = code.strip().strip("\x00")
                station["carid_status"] = "ok" if station["carid"] else "empty"

    async def _messages(self, client: httpx.AsyncClient, pid: int, index: int, cell: Any,
                        flags: dict, cell_error: bool | None, stopped: bool | None, station: dict) -> None:
        path = Path(__file__).resolve().parent / "static" / "gwm-mb-alarm-map.json"
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            station["diagnostics_complete"] = False
            return
        if mapping.get("plc") != "GWM_MB":
            station["diagnostics_complete"] = False
            return
        candidates = [item for item in mapping.get("alarms", [])
                      if ((item.get("scope") == "station" and item.get("index") == index)
                          or (item.get("scope") == "cell" and item.get("index") == cell))]
        root = self.roots.get((pid, "MSG", "W"))
        nodes = {word: await self._numeric_element(client, pid, root, word)
                 for word in sorted({item["word"] for item in candidates})}
        values = await self._values(client, pid, list(nodes.values()))
        station["diagnostics_complete"] &= all(
            isinstance(good_value(values, node), int) and not isinstance(good_value(values, node), bool)
            and 0 <= good_value(values, node) <= 65535 for node in nodes.values())
        for item in candidates:
            raw = good_value(values, nodes.get(item["word"]))
            if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 65535:
                continue
            if not (raw & (1 << item["bit"])):
                continue
            gated = flags.get(item.get("category")) is True if item["scope"] == "station" else cell_error is True
            station["alarms"].append({"text": item["text"], "source": f"MSG.W[{item['word']}].{item['bit']}",
                                      "is_cause": stopped is True and gated, "word": item["word"], "bit": item["bit"]})

    async def _safe_group(self, client: httpx.AsyncClient, name: str, output: dict, selected: str | None) -> None:
        try:
            await self._basic(client, name, output)
            if selected in GROUPS[name]:
                await self._details(client, selected, output)
        except asyncio.CancelledError:
            next(item for item in output["plcs"] if item["name"] == name)["error"] = "Обнаружение сигналов продолжается или истёк срок ответа ПЛК"
            if selected in GROUPS[name]:
                output["stations"][selected]["diagnostics_complete"] = False
                output["stations"][selected]["details_error"] = "Сигналы диагностики ещё недоступны"
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            next(item for item in output["plcs"] if item["name"] == name)["error"] = "Не удалось прочитать сигналы ПЛК"
            if selected in GROUPS[name]:
                output["stations"][selected]["diagnostics_complete"] = False
                output["stations"][selected]["details_error"] = "Не удалось прочитать диагностику ПЛК"

    async def snapshot(self, selected: str | None) -> dict:
        async with self.lock:
            if self.last is not None and selected == self.last_station and time.monotonic() - self.last_at < 1:
                return copy.deepcopy(self.last)
            output = blank_snapshot()
            await self._metadata()
            async with httpx.AsyncClient(timeout=httpx.Timeout(4.5, connect=2)) as client:
                try:
                    await asyncio.wait_for(asyncio.gather(*(self._safe_group(client, name, output, selected) for name in GROUPS)), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            output["updated_at"] = _now()
            self.last, self.last_station, self.last_at = copy.deepcopy(output), selected, time.monotonic()
            return output


@router.get("/gwm")
async def gwm_snapshot(request: Request, station: str | None = Query(None, max_length=10)) -> dict:
    if station is not None and not any(station in group for group in GROUPS.values()):
        raise HTTPException(status_code=422, detail="Неизвестная станция GWM")
    if not hasattr(request.app.state, "gwm_scada"):
        request.app.state.gwm_scada = GwmService(request.app.state.pool)
    try:
        return await asyncio.wait_for(request.app.state.gwm_scada.snapshot(station), timeout=5.8)
    except asyncio.TimeoutError:
        output = blank_snapshot()
        for plc in output["plcs"]:
            plc["error"] = "Истёк срок ответа. Следующий опрос повторит чтение"
        return output
