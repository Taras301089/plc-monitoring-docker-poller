"""Сканер ПЛК (python-async-poller/opc_client.py): переменные-структуры и массивы должны помечаться,
чтобы интерфейс «Переменные ПЛК» мог раскрыть их по клику. Тесты идут на подставном дереве OPC UA, без связи с ПЛК."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent / "python-async-poller"))   # в конец: у поллера свой main.py, он не должен перекрывать main.py API

from asyncua import ua  # noqa: E402

from models import Plc  # noqa: E402
from opc_client import PlcOpcClient  # noqa: E402


def _ref(path: str, name: str, cls: ua.NodeClass):
    return SimpleNamespace(
        NodeId=ua.NodeId.from_string(f"ns=3;s={path}"),
        BrowseName=ua.QualifiedName(name, 3),
        NodeClass=cls,
    )


class FakeNode:
    def __init__(self, tree, key):
        self._tree, self._key = tree, key

    async def get_references(self, refs=None, direction=None):
        return self._tree.get(self._key, [])


class FakeClient:
    def __init__(self, tree):
        self._tree = tree

    def get_objects_node(self):
        return FakeNode(self._tree, "ROOT")

    def get_node(self, node_id):
        return FakeNode(self._tree, node_id.to_string())


V, O = ua.NodeClass.Variable, ua.NodeClass.Object


def _tree():
    ib = '"Energy_DRV_FB_IDB".Inputs'
    return {
        "ROOT": [_ref("CPU", "CPU", O)],
        "ns=3;s=CPU": [_ref("DB", "DataBlocksInstance", O)],
        "ns=3;s=DB": [_ref('"Energy_DRV_FB_IDB"', "Energy_DRV_FB_IDB", O)],
        'ns=3;s="Energy_DRV_FB_IDB"': [_ref(ib, "Inputs", O)],
        f"ns=3;s={ib}": [
            _ref(f'"Energy_DRV_FB_IDB"."Status_Data_I"', "Status_Data_I", V),
            _ref(f'"Energy_DRV_FB_IDB"."Connection_Type"', "Connection_Type", V),
            _ref(f'"Energy_DRV_FB_IDB"."Phases"', "Phases", V),
        ],
        # структура: поля-переменные
        'ns=3;s="Energy_DRV_FB_IDB"."Status_Data_I"': [
            _ref('"Energy_DRV_FB_IDB"."Status_Data_I"."Voltage L1-N"', "Voltage L1-N", V),
            _ref('"Energy_DRV_FB_IDB"."Status_Data_I"."Current L1"', "Current L1", V),
        ],
        # массив: элементы [0], [1] (имя узла оканчивается на [<имя>])
        'ns=3;s="Energy_DRV_FB_IDB"."Phases"': [
            _ref('"Energy_DRV_FB_IDB"."Phases"[0]', "0", V),
            _ref('"Energy_DRV_FB_IDB"."Phases"[1]', "1", V),
        ],
    }


@pytest.fixture
async def scanned():
    c = PlcOpcClient(Plc(id=1, name="T", opc_endpoint="opc.tcp://x", poll_interval_ms=1000))
    c._client = FakeClient(_tree())

    async def connected():
        return True

    async def no_globals(client, db_filter):
        return []

    c._ensure_connected_unlocked = connected
    c._get_global_data_blocks_by_nodeid = no_globals
    rows = await c.browse_variables()
    return {r["variable_name"]: r for r in rows if r["node_class"] == "Variable"}


async def test_structure_variable_is_marked(scanned):
    assert scanned["Status_Data_I"]["data_type"] == "Structure tag"


async def test_array_variable_is_marked(scanned):
    assert scanned["Phases"]["data_type"] == "Array"


async def test_plain_variable_stays_unknown(scanned):
    assert scanned["Connection_Type"]["data_type"] == "Unknown"


async def test_struct_fields_not_stored_up_front(scanned):
    """Поля структуры не раздувают базу: они подгружаются по клику (запрос children)."""
    assert "Voltage L1-N" not in scanned
