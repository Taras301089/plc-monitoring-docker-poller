"""Журнал аварий, шаг 1: словарь аварий (разбор .xlsx, адрес бита, импорт и чтение через API)."""
import io
import time
import zipfile
from pathlib import Path

import pytest

REAL_FILE = Path(r"C:\plc-monitoring-docker-poller\tasks\data\HMIAlarms_GWM_UB_Eng.xlsx")
_n = 0


def _alarms():
    import alarms
    return alarms


def make_xlsx(rows, shared=True):
    """Минимальный xlsx с префиксом x:, общими строками и пропущенными пустыми ячейками.
    rows: список словарей {колонка: значение}; число = t="n", строка = общая строка."""
    sst: list[str] = []

    def si(s):
        if s not in sst:
            sst.append(s)
        return sst.index(s)

    xml_rows = []
    for r, cells in enumerate(rows, start=1):
        cs = []
        for col, val in cells.items():
            if isinstance(val, (int, float)):
                cs.append(f'<x:c r="{col}{r}" t="n"><x:v>{val}</x:v></x:c>')
            else:
                cs.append(f'<x:c r="{col}{r}" t="s"><x:v>{si(val)}</x:v></x:c>')
        xml_rows.append(f'<x:row r="{r}">{"".join(cs)}</x:row>')
    ns = 'xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = f'<?xml version="1.0" encoding="utf-8"?><x:worksheet {ns}><x:sheetData>{"".join(xml_rows)}</x:sheetData></x:worksheet>'
    strings = "".join(f'<x:si><x:t xml:space="preserve">{s}</x:t></x:si>' for s in sst)
    sst_xml = f'<?xml version="1.0" encoding="utf-8"?><x:sst {ns}>{strings}</x:sst>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst_xml)
    return buf.getvalue()


HEADER = {"A": "ID", "B": "Name", "C": "Alarm text [en-US], Alarm text", "E": "Class", "F": "Trigger tag", "G": "Trigger bit"}


def small_xlsx():
    return make_xlsx([
        HEADER,
        {"A": 1, "B": "Discrete_alarm_1", "C": "1_Device\u00a0profinet  error ", "E": "Errors", "F": "err_word", "G": 0},
        {"A": 2, "B": "Discrete_alarm_2", "C": "Без префикса", "E": "Errors", "F": "err_word", "G": 16},   # D пропущена
        {"A": 3, "B": "Discrete_alarm_3", "C": "5_", "E": "Errors", "F": "err_word", "G": 15},          # пустышка
        {"A": 4, "B": "Discrete_alarm_4", "E": "Errors", "F": "err_word", "G": 17},                      # без текста
    ])


# ------------------------------------------------------------------ чистые функции
def test_bit_address():
    a = _alarms()
    assert a.bit_address(0) == (0, 0)
    assert a.bit_address(15) == (0, 15)
    assert a.bit_address(16) == (1, 0)
    assert a.bit_address(11999) == (749, 15)


def test_normalize_text():
    n = _alarms().normalize_text
    assert n("1_Device profinet") == ("1", "Device profinet")
    assert n("  12_\u00a0Двигатель\u2009 перегрет \t") == ("12", "Двигатель перегрет")
    assert n("11998_") == ("11998", "")
    assert n("Без префикса") == ("", "Без префикса")
    assert n("2 Not a prefix_x") == ("", "2 Not a prefix_x")
    assert n(None) == ("", "")


def test_parse_small_xlsx():
    items = _alarms().parse_alarm_xlsx(small_xlsx())
    assert [i["bit_index"] for i in items] == [0, 15, 16, 17]
    by = {i["bit_index"]: i for i in items}
    assert by[0]["code"] == "1" and by[0]["text"] == "Device profinet error" and not by[0]["is_placeholder"]
    assert (by[0]["word_index"], by[0]["bit_in_word"]) == (0, 0)
    assert by[0]["class"] == "Errors" and by[0]["trigger_tag"] == "err_word" and by[0]["alarm_name"] == "Discrete_alarm_1"
    assert by[15]["code"] == "5" and by[15]["text"] == "" and by[15]["is_placeholder"]
    assert by[16]["code"] == "" and by[16]["text"] == "Без префикса"
    assert (by[16]["word_index"], by[16]["bit_in_word"]) == (1, 0)
    assert by[17]["is_placeholder"]


def test_parse_errors():
    a = _alarms()
    with pytest.raises(ValueError):
        a.parse_alarm_xlsx(b"not a zip")
    with pytest.raises(ValueError):
        a.parse_alarm_xlsx(make_xlsx([HEADER]))   # нет аварий


# ------------------------------------------------------------------ API
async def _plc(pool):
    global _n
    _n += 1
    return await pool.fetchval(
        "INSERT INTO plcs (name, opc_endpoint, is_active) VALUES ($1, 'opc.tcp://10.0.0.1:4840', TRUE) RETURNING id", f"al{_n}")


def _post(client, plc_id, data):
    return client.post(f"/api/alarms/catalog/import?plc_id={plc_id}", content=data,
                       headers={"Content-Type": "application/octet-stream"})


async def test_import_access(users, anon, pool):
    plc = await _plc(pool)
    r = await _post(users["t_admin"], plc, small_xlsx())
    assert r.status_code == 200, r.text
    assert r.json() == {"imported": 4, "placeholders": 2, "plc_id": plc}
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        assert (await _post(users[login], plc, small_xlsx())).status_code == 403, login
    assert (await _post(anon, plc, small_xlsx())).status_code == 401


async def test_import_errors(users, pool):
    plc = await _plc(pool)
    assert (await _post(users["t_admin"], plc + 1000, small_xlsx())).status_code == 404
    r = await _post(users["t_admin"], plc, "это не xlsx".encode())
    assert r.status_code == 422 and "Excel" in r.json()["detail"]
    assert await pool.fetchval("SELECT count(*) FROM alarm_catalog WHERE plc_id = $1", plc) == 0


async def test_import_too_large(users, pool, monkeypatch):
    import alarms
    monkeypatch.setattr(alarms, "MAX_UPLOAD", 100)
    plc = await _plc(pool)
    assert (await _post(users["t_admin"], plc, small_xlsx())).status_code == 413


async def test_reimport_replaces(users, pool):
    plc = await _plc(pool)
    other = await _plc(pool)
    admin = users["t_admin"]
    assert (await _post(admin, other, small_xlsx())).status_code == 200
    assert (await _post(admin, plc, small_xlsx())).status_code == 200
    assert (await _post(admin, plc, small_xlsx())).status_code == 200
    assert await pool.fetchval("SELECT count(*) FROM alarm_catalog WHERE plc_id = $1", plc) == 4
    new = make_xlsx([HEADER, {"A": 1, "B": "X", "C": "7_Новая", "E": "Errors", "F": "w", "G": 3}])
    r = await _post(admin, plc, new)
    assert r.json()["imported"] == 1
    rows = await pool.fetch("SELECT bit_index, code, text FROM alarm_catalog WHERE plc_id = $1", plc)
    assert [(x["bit_index"], x["code"], x["text"]) for x in rows] == [(3, "7", "Новая")]
    assert await pool.fetchval("SELECT count(*) FROM alarm_catalog WHERE plc_id = $1", other) == 4   # чужой ПЛК цел


async def test_get_catalog(users, anon, pool):
    plc = await _plc(pool)
    rows = [HEADER] + [
        {"A": i + 1, "B": f"Discrete_alarm_{i + 1}", "C": f"{i + 1}_Мотор {i}" if i % 2 == 0 else f"{i + 1}_", "E": "Errors", "F": "err_word", "G": i}
        for i in range(10)
    ]
    assert (await _post(users["t_admin"], plc, make_xlsx(rows))).status_code == 200
    v = users["t_viewer"]
    r = (await v.get(f"/api/alarms/catalog?plc_id={plc}")).json()
    assert r["total"] == 5 and r["all"] == 10 and r["placeholders"] == 5 and len(r["items"]) == 5
    assert all(not i["is_placeholder"] for i in r["items"])
    r = (await v.get(f"/api/alarms/catalog?plc_id={plc}&include_placeholders=true")).json()
    assert r["total"] == 10
    r = (await v.get(f"/api/alarms/catalog?plc_id={plc}&limit=2&offset=2")).json()
    assert [i["bit_index"] for i in r["items"]] == [4, 6] and r["total"] == 5
    r = (await v.get(f"/api/alarms/catalog?plc_id={plc}&q=мотор 8")).json()
    assert [i["bit_index"] for i in r["items"]] == [8] and r["total"] == 1
    r = (await v.get(f"/api/alarms/catalog?plc_id={plc}&q=%")).json()
    assert r["total"] == 0
    assert (await anon.get(f"/api/alarms/catalog?plc_id={plc}")).status_code == 401


# ------------------------------------------------------------------ реальный файл (если есть)
@pytest.mark.skipif(not REAL_FILE.exists(), reason="реального файла аварий нет")
def test_real_file():
    t = time.time()
    items = _alarms().parse_alarm_xlsx(REAL_FILE.read_bytes())
    print(f"разбор реального файла: {time.time() - t:.1f} с")
    assert len(items) == 12000
    assert [i["bit_index"] for i in items] == list(range(12000))
    first = items[0]
    assert first["code"] == "1" and first["text"] and not first["is_placeholder"]
    assert first["text"].startswith(("Device", "1_Device")) or "Device" in first["text"]
    assert not first["text"].startswith("1_")
