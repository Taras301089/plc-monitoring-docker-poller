"""Read-only SCADA binding/quality regressions. No real PLC or database required."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from Scada.gwm import api as gwm
from Scada.gwm import SCREEN_URL, STATIC_DIR, mount


def child(name, node):
    return {"variable_name": str(name), "node_id": node}


def good(value):
    return {"ok": True, "v": value, "t": "String" if isinstance(value, str) else "Int16", "s": ""}


class Poller:
    def __init__(self):
        self.tree, self.values, self.requests = {}, {}, []

    def handle(self, request):
        self.requests.append(request)
        if request.method == "GET":
            self_node = request.url.params["node_id"]
            payload = self.tree.get(self_node, [])
        else:
            nodes = json.loads(request.content)["node_ids"]
            if len(nodes) > 200:
                raise AssertionError("Poller truncates requests exceeding 200 nodes")
            payload = {"values": {node: self.values.get(node, {"ok": False, "v": None}) for node in nodes}}
        return httpx.Response(200, json=payload, request=request)

    def fields(self, parent, fields):
        self.tree[parent] = [child(name, f"{parent}/{name}") for name in fields]


class BindingTests(unittest.TestCase):
    def test_numeric_sort_and_missing_indices_fail_closed(self):
        self.assertEqual([r["node_id"] for r in gwm.indexed_children([child(2, "c"), child(0, "a"), child(1, "b")])], ["a", "b", "c"])
        self.assertEqual(gwm.indexed_children([child(0, "a"), child(2, "c")]), [])
        self.assertEqual(gwm.indexed_children([child(1, "b"), child(2, "c")]), [])
        self.assertEqual(gwm.indexed_children([child(0, "a"), child(0, "b")]), [])

    def test_unknown_and_false_are_not_the_same(self):
        self.assertIsNone(gwm.bool_value(None))
        self.assertIsNone(gwm.bool_value("false"))
        self.assertIs(gwm.bool_value(0), False)
        self.assertIsNone(gwm.good_value({"x": {"ok": False, "v": True}}, "x"))
        snapshot = gwm.blank_snapshot()
        self.assertEqual(len(snapshot["stations"]), 27)
        self.assertEqual(snapshot["stations"]["MB130"]["binding"]["color_index"], 13)
        self.assertEqual(snapshot["stations"]["MB140"]["binding"]["color_index"], 1)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        mount(self.app)
        # Compatibility routes must take precedence over the shared mount.
        self.app.mount("/static", StaticFiles(directory=str(STATIC_DIR)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://scada.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_canonical_screen_assets_and_old_links(self):
        screen = await self.client.get(SCREEN_URL)
        self.assertEqual(screen.status_code, 200)
        self.assertIn('href="gwm-scada.css"', screen.text)
        for filename in ("gwm-scada.html", "gwm-scada.css", "gwm-scada.js", "gwm-mb-alarm-map.json"):
            self.assertEqual((await self.client.get(f"/scada/gwm/{filename}")).status_code, 200)
            for prefix in ("/static/", "/"):
                response = await self.client.get(prefix + filename)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers["location"], f"/scada/gwm/{filename}")
        self.assertEqual((await self.client.get("/scada/gwm/")).headers["location"], SCREEN_URL)

    async def test_api_route_retains_contract_without_connecting_to_plc(self):
        class FakeService:
            async def snapshot(self, selected):
                return {"stations": {}, "plcs": [], "updated_at": "test", "selected": selected}
        self.app.state.gwm_scada = FakeService()
        response = await self.client.get("/api/scada/gwm?station=MB200")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected"], "MB200")
        self.assertEqual((await self.client.get("/api/scada/gwm?station=MB999")).status_code, 422)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_detail_read_cannot_claim_complete_diagnostics(self):
        service = gwm.GwmService(None)
        output = gwm.blank_snapshot()

        async def basic(client, name, result):
            next(item for item in result["plcs"] if item["name"] == name)["ok"] = True

        async def details(client, sid, result):
            result["stations"][sid]["diagnostics_complete"] = True
            await asyncio.sleep(1)

        service._basic, service._details = basic, details
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(service._safe_group(None, "GWM_MB", output, "MB200"), timeout=0.01)
        self.assertFalse(output["stations"]["MB200"]["diagnostics_complete"])
        self.assertFalse(output["stations"]["MB200"]["details_loaded"])
        self.assertTrue(next(item for item in output["plcs"] if item["name"] == "GWM_MB")["ok"])

    async def test_all_station_bindings_use_discovered_ordinal_and_no_db_or_plc_writes(self):
        service, poller = gwm.GwmService(None), Poller()
        for pid, name in enumerate(gwm.GROUPS, 10):
            service.plcs[name] = {"id": pid, "name": name, "is_active": True, "opc_endpoint": "opc.tcp://example:4840"}
            service.roots[pid, "user", "STN_Screen color"] = f"color{pid}"
            service.roots[pid, "Stn_Trans", "STN"] = f"trans{pid}"
            stations = gwm.GROUPS[name]
            poller.values[f"color{pid}"] = good([i % 4 + 1 for i in range(len(stations))])
            poller.tree[f"trans{pid}"] = [child(i, f"T{pid}.{i}") for i in reversed(range(len(stations)))]
            for i in range(len(stations)):
                parent = f"T{pid}.{i}"
                poller.fields(parent, ["RUN_FWD", "RUN_BWD", *gwm.SENSORS.values()])
                for field in ["RUN_FWD", "RUN_BWD", *gwm.SENSORS.values()]:
                    poller.values[f"{parent}/{field}"] = good(i % 2 == 0)
        output = gwm.blank_snapshot()
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            await asyncio.gather(*(service._basic(client, name, output) for name in gwm.GROUPS))
        for name, stations in gwm.GROUPS.items():
            for i, sid in enumerate(stations):
                self.assertEqual(output["stations"][sid]["color"], i % 4 + 1)
                self.assertIs(output["stations"][sid]["RUN_FWD"], i % 2 == 0)
                self.assertIs(output["stations"][sid]["sensors"]["InLet"], i % 2 == 0)
        self.assertTrue(all(p["ok"] for p in output["plcs"]))
        self.assertTrue(all(r.method == "GET" or r.url.path.endswith("/opcua/values") for r in poller.requests))

    async def test_batch_limit_and_failed_quality(self):
        service, poller = gwm.GwmService(None), Poller()
        for i in range(405):
            poller.values[str(i)] = good(i)
        poller.values["4"] = {"ok": False, "v": 99}
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            result = await service._values(client, 5, [str(i) for i in range(405)] + [None, "0"])
        self.assertEqual(len(result), 405)
        self.assertEqual([len(json.loads(r.content)["node_ids"]) for r in poller.requests], [200, 200, 5])
        self.assertIsNone(gwm.good_value(result, "4"))

    async def test_cell_uses_actual_index_but_station_and_vin_use_ordinal(self):
        service, poller = gwm.GwmService(None), Poller()
        poller.tree["array"] = [child(i, f"node{i}") for i in range(11)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            self.assertEqual(await service._numeric_element(client, 1, "array", 2), "node2")
            self.assertEqual(await service._element(client, 1, "array", 6), "node6")
            self.assertIsNone(await service._numeric_element(client, 1, "array", 12))
        self.assertEqual(len(poller.requests), 1, "Discovery should be reused")

    async def test_carid_is_current_pointer_and_stale_ew_is_never_read(self):
        service, poller = gwm.GwmService(None), Poller()
        name, pid = "GWM_MB", 6
        service.plcs[name] = {"id": pid, "is_active": True}
        for db, field, node in [("Stn", "STN", "stn"), ("Stn_Trans", "STN", "trans"), ("Cell", "CELL", "cells"), ("Car_Stn", "STN_C", "cars"), ("BODY_SN", "VIN", "vins")]:
            service.roots[pid, db, field] = node
        for root, length in [("stn", 14), ("trans", 14), ("cells", 11), ("cars", 14), ("vins", 100)]:
            poller.tree[root] = [child(i, f"{root}{i}") for i in range(length)]
        poller.fields("stn6", ["EW1", "eW", "zW", "CFG"])
        poller.fields("trans6", ["NoSkid"])
        poller.fields("stn6/EW1", gwm.ERROR_LABELS)
        poller.fields("stn6/zW", ["Err_Stop"])
        poller.fields("stn6/CFG", ["Cell"])
        poller.fields("cells2", ["zw"])
        poller.fields("cells2/zw", ["ErrH"])
        poller.fields("cars6", ["CarType"])
        poller.fields("cars6/CarType", ["P1"])
        poller.fields("vins12", ["Code"])
        for field in gwm.ERROR_LABELS:
            poller.values[f"stn6/EW1/{field}"] = good(field == "Driver")
        poller.values.update({"stn6/zW/Err_Stop": good(True), "stn6/CFG/Cell": good(2), "trans6/NoSkid": good(False),
                              "cells2/zw/ErrH": good(False), "cars6/CarType/P1": good(12), "vins12/Code": good("CURRENT-CAR-12345")})
        output = gwm.blank_snapshot()
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            await service._details(client, "MB200", output)
        result = output["stations"]["MB200"]
        self.assertEqual(result["carid"], "CURRENT-CAR-12345")
        self.assertEqual(result["carid_status"], "ok")
        self.assertEqual([item["text"] for item in result["causes"]], ["Привод"])
        self.assertIs(result["causes"][0]["is_cause"], True)
        requested = " ".join(str(r.url) + r.content.decode() for r in poller.requests)
        self.assertNotIn("eW/", requested)
        self.assertNotIn("Memory", requested)

    async def test_retained_car_pointer_is_not_displayed_without_skid_occupancy(self):
        service, poller = gwm.GwmService(None), Poller()
        service.roots[6, "Stn_Trans", "STN"] = "trans"
        poller.tree["trans"] = [child(0, "station0")]
        poller.fields("station0", ["NoSkid"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            for value, status in [(True, "empty"), (None, "unavailable")]:
                poller.values["station0/NoSkid"] = good(value)
                station = gwm._blank_details()
                await service._carid(client, 6, 0, station)
                self.assertEqual(station["carid_status"], status)
                self.assertIsNone(station["carid"])

    async def test_alarm_word_is_a_cause_only_when_its_confirmed_stop_category_is_active(self):
        service, poller = gwm.GwmService(None), Poller()
        service.roots[6, "MSG", "W"] = "words"
        poller.tree["words"] = [child(i, f"word{i}") for i in range(1000)]
        for i in range(1000):
            poller.values[f"word{i}"] = good(0)
        poller.values["word64"] = good(1 << 7)
        station = {"diagnostics_complete": True, "alarms": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(poller.handle)) as client:
            await service._messages(client, 6, 7, 2, {"Part": False}, False, True, station)
            first = next(item for item in station["alarms"] if item["word"] == 64 and item["bit"] == 7)
            self.assertIs(first["is_cause"], False)
            station["alarms"] = []
            await service._messages(client, 6, 7, 2, {"Part": True}, False, True, station)
            second = next(item for item in station["alarms"] if item["word"] == 64 and item["bit"] == 7)
            self.assertIs(second["is_cause"], True)
            self.assertIn("MB200", second["text"])
            station["alarms"] = []
            poller.values["word64"] = {"ok": False, "v": 1 << 7}
            await service._messages(client, 6, 7, 2, {"Part": True}, False, True, station)
            self.assertFalse(station["diagnostics_complete"])
            self.assertEqual(station["alarms"], [])


if __name__ == "__main__":
    unittest.main()
