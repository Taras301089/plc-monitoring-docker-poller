"""Ответственность за отставание, шаг 2 (T-15): ответ отдела ОТО / ИТО на строку простоя.

«Согласен»: все минуты строки у отдела, «✓»; «Частично»: отдел принимает k минут, остаток к ПР, «✓»; «Не согласен» (комментарий обязателен):
минуты остаются у отдела, «!» спорно; без ответа «…» ждёт подтверждения. Права проверяет сервер, ответ пишется в журнал."""
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import dayflags
import downtime
from test_responsibility import DAY, _item, _put, _seed
from test_responsibility_totals import _block

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
JS = STATIC / "dtresponse.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    from datetime import date
    monkeypatch.setattr(dayflags, "plant_today", lambda: date(2026, 10, 12))


async def _seed_items(pool, users, owners=("oto",), minutes=(10,)):
    scr, dt, r_def, _ = await _seed(pool)
    assert (await _put(users["t_master"], dt, [_item(r_def, m, owner=o, note="нет питания" if o == "other" else "")
                                                for o, m in zip(owners, minutes)])).status_code == 200
    ids = [r["id"] for r in await pool.fetch("SELECT id FROM kpi_downtime_items WHERE downtime_id = $1 ORDER BY id", dt)]
    return scr, dt, ids


def _url(iid):
    return f"/api/downtime-items/{iid}/response"


async def _day_items(client, scr):
    r = await client.get(f"/api/kpi/screens/{scr}/downtimes", params={"date": DAY.isoformat()})
    assert r.status_code == 200
    return {i["id"]: i for i in r.json()["downtimes"][0]["items"]}


async def _resp(client, scr):
    r = await client.get(f"/api/kpi/screens/{scr}/responsibility", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    assert r.status_code == 200
    return r.json()


class _dept:
    """Временно меняет подразделение тестового пользователя (пользователи общие на сессию): возвращает прежнее после теста."""

    def __init__(self, pool, login, dept):
        self.pool, self.login, self.dept = pool, login, dept

    async def __aenter__(self):
        self.old = await self.pool.fetchval("SELECT department FROM app_users WHERE login = $1", self.login)
        await self.pool.execute("UPDATE app_users SET department = $2 WHERE login = $1", self.login, self.dept)

    async def __aexit__(self, *a):
        await self.pool.execute("UPDATE app_users SET department = $2 WHERE login = $1", self.login, self.old)


# ---------- схема ----------

async def test_schema_idempotent(app, pool):
    for _ in range(2):
        await downtime.ensure_downtime_schema(pool)
    assert await pool.fetchval("SELECT to_regclass('kpi_downtime_responses')") is not None
    assert await pool.fetchval("SELECT to_regclass('kpi_downtime_responses_log')") is not None
    cols = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'kpi_downtime_responses'")}
    assert {"id", "item_id", "department", "answer", "minutes", "comment", "user_id", "user_name", "created_at", "updated_at"} <= cols
    # уникальность по строке и удаление вместе со строкой описания
    con = await pool.fetchval(
        "SELECT confdeltype::text FROM pg_constraint WHERE conrelid = 'kpi_downtime_responses'::regclass AND contype = 'f'")
    assert con == "c"
    assert await pool.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'kpi_downtime_responses'::regclass AND contype = 'u'") == 1


# ---------- права ----------

async def test_oto_can_answer_oto_not_ito_nor_production(app, users, pool):
    scr, dt, (i_oto, i_ito, i_pr) = await _seed_items(pool, users, ("oto", "ito", "production"), (5, 5, 5))
    oto = users["t_oto"]
    assert (await oto.put(_url(i_oto), json={"answer": "agree"})).status_code == 200
    r = await oto.put(_url(i_ito), json={"answer": "agree"})
    assert r.status_code == 403 and "ИТО" in r.json()["detail"]
    r = await oto.put(_url(i_pr), json={"answer": "agree"})
    assert r.status_code == 422 and "ОТО и ИТО" in r.json()["detail"]          # у ПР и ДР ответов нет


async def test_ito_department_answers_ito(app, users, pool):
    scr, dt, (i_oto, i_ito) = await _seed_items(pool, users, ("oto", "ito"), (5, 5))
    async with _dept(pool, "t_oto", "ito"):
        assert (await users["t_oto"].put(_url(i_ito), json={"answer": "agree"})).status_code == 200
        assert (await users["t_oto"].put(_url(i_oto), json={"answer": "agree"})).status_code == 403


async def test_production_and_viewer_cannot_answer(app, users, pool):
    scr, dt, (iid,) = await _seed_items(pool, users)
    for login in ("t_master", "t_area"):                          # подразделение «production»
        r = await users[login].put(_url(iid), json={"answer": "agree"})
        assert r.status_code == 403 and "ОТО" in r.json()["detail"], login
    async with _dept(pool, "t_viewer", "oto"):                     # «Просмотр» не отвечает даже из подразделения ОТО
        r = await users["t_viewer"].put(_url(iid), json={"answer": "agree"})
        assert r.status_code == 403 and "Просмотр" in r.json()["detail"]


async def test_admin_and_chief_can_answer_any_department(app, users, pool):
    scr, dt, (i_oto, i_ito) = await _seed_items(pool, users, ("oto", "ito"), (5, 5))
    assert (await users["t_admin"].put(_url(i_oto), json={"answer": "agree"})).status_code == 200
    assert (await users["t_chief"].put(_url(i_ito), json={"answer": "disagree", "comment": "датчик исправен"})).status_code == 200


async def test_anonymous_cannot_answer(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users)
    assert (await anon.put(_url(iid), json={"answer": "agree"})).status_code == 401
    assert (await users["t_oto"].put("/api/downtime-items/999999/response", json={"answer": "agree"})).status_code == 404


# ---------- ответы и валидация ----------

async def test_agree_confirms_all_minutes(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users, ("oto",), (12,))
    assert (await _resp(anon, scr))["pending"] == 12
    r = await users["t_oto"].put(_url(iid), json={"answer": "agree"})
    assert r.status_code == 200
    it = (await _day_items(anon, scr))[iid]
    assert it["owner_status"] == "confirmed"
    assert it["response"]["answer"] == "agree" and it["response"]["minutes"] is None and it["response"]["user_name"]
    assert it["response"]["author"].startswith("T_OTO") and it["response"]["updated_at"]
    t = await _resp(anon, scr)
    assert t["owners"]["oto"] == 12 and t["pending"] == 0 and t["disputed"] == 0


async def test_partial_moves_rest_to_production(app, users, anon, pool):
    scr, dt, (i_oto, i_pr) = await _seed_items(pool, users, ("oto", "production"), (10, 5))
    r = await users["t_oto"].put(_url(i_oto), json={"answer": "partial", "minutes": 3, "comment": "часть наша"})
    assert r.status_code == 200
    it = (await _day_items(anon, scr))[i_oto]
    assert it["owner_status"] == "confirmed" and it["response"]["minutes"] == 3
    t = await _resp(anon, scr)
    assert t["total"] == 30 and t["owners"]["oto"] == 3 and t["owners"]["production"] == 12 and t["undescribed"] == 15
    assert t["pending"] == 0
    assert round(sum(t["owners"].values()) + t["undescribed"], 1) == t["total"]


@pytest.mark.parametrize("minutes", [None, 0, 10, 11, -1])
async def test_partial_needs_minutes_between_zero_and_row(app, users, pool, minutes):
    scr, dt, (iid,) = await _seed_items(pool, users, ("oto",), (10,))
    body = {"answer": "partial"} if minutes is None else {"answer": "partial", "minutes": minutes}
    r = await users["t_oto"].put(_url(iid), json=body)
    assert r.status_code == 422
    assert await pool.fetchval("SELECT count(*) FROM kpi_downtime_responses") == 0


async def test_disagree_needs_comment_and_is_disputed(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users, ("oto",), (10,))
    r = await users["t_oto"].put(_url(iid), json={"answer": "disagree", "comment": "   "})
    assert r.status_code == 422 and "комментарий" in r.json()["detail"].lower()
    assert (await users["t_oto"].put(_url(iid), json={"answer": "disagree", "comment": "не наша поломка"})).status_code == 200
    it = (await _day_items(anon, scr))[iid]
    assert it["owner_status"] == "disputed" and it["response"]["comment"] == "не наша поломка"
    t = await _resp(anon, scr)
    assert t["owners"]["oto"] == 10 and t["disputed"] == 10 and t["disputed_by"]["oto"] == 10 and t["pending"] == 0
    assert downtime.owner_label("oto", it["owner_status"]) == "ОТО !"


async def test_no_answer_is_pending(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users, ("ito",), (7,))
    it = (await _day_items(anon, scr))[iid]
    assert it["owner_status"] == "pending" and it["response"] is None
    assert downtime.owner_status("oto", None) == "pending" and downtime.owner_status("oto", "agree") == "confirmed"
    assert downtime.owner_status("ito", "partial") == "confirmed" and downtime.owner_status("ito", "disagree") == "disputed"
    assert downtime.owner_status("production", "disagree") == "confirmed"       # ПР и ДР всегда «подтверждено»


async def test_answer_of_other_department_is_ignored_after_owner_change(app, users, anon, pool):
    """Сменили ответственного ОТО → ИТО: прежний ответ ОТО к ИТО не относится, строка снова ждёт подтверждения."""
    scr, dt, (iid,) = await _seed_items(pool, users, ("oto",), (10,))
    await users["t_oto"].put(_url(iid), json={"answer": "disagree", "comment": "не наше"})
    r_def = await pool.fetchval("SELECT reason_id FROM kpi_downtime_items WHERE id = $1", iid)
    assert (await _put(users["t_master"], dt, [_item(r_def, 10, id=iid, owner="ito")])).status_code == 200
    it = (await _day_items(anon, scr))[iid]
    assert it["owner_status"] == "pending" and it["response"] is None
    assert (await _resp(anon, scr))["disputed"] == 0


# ---------- изменение, отзыв, журнал ----------

async def test_answer_can_be_changed_later_and_is_logged(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users, ("oto",), (10,))
    oto = users["t_oto"]
    await oto.put(_url(iid), json={"answer": "disagree", "comment": "не наше"})
    # «на следующий день»: срока нет, ответ меняется в любой день
    await pool.execute("UPDATE kpi_downtime_responses SET created_at = created_at - interval '3 days', updated_at = updated_at - interval '3 days'")
    assert (await oto.put(_url(iid), json={"answer": "partial", "minutes": 4})).status_code == 200
    row = await pool.fetchrow("SELECT answer, minutes, created_at < updated_at - interval '1 day' AS later FROM kpi_downtime_responses WHERE item_id = $1", iid)
    assert row["answer"] == "partial" and float(row["minutes"]) == 4 and row["later"]
    log = await pool.fetch("SELECT action, old_answer, new_answer, new_minutes, changed_by_name FROM kpi_downtime_responses_log WHERE item_id = $1 ORDER BY id", iid)
    assert [(r["action"], r["old_answer"], r["new_answer"]) for r in log] == [("create", None, "disagree"), ("update", "disagree", "partial")]
    assert float(log[1]["new_minutes"]) == 4 and log[1]["changed_by_name"]


async def test_withdraw_only_author_or_admin(app, users, anon, pool):
    scr, dt, (i1, i2) = await _seed_items(pool, users, ("oto", "oto"), (5, 5))
    await users["t_oto"].put(_url(i1), json={"answer": "agree"})
    await users["t_oto"].put(_url(i2), json={"answer": "agree"})
    r = await users["t_chief"].delete(_url(i1))
    assert r.status_code == 403 and "администратор" in r.json()["detail"]
    assert (await users["t_oto"].delete(_url(i1))).status_code == 200            # автор ответа
    assert (await users["t_admin"].delete(_url(i2))).status_code == 200          # администратор
    assert (await users["t_admin"].delete(_url(i2))).status_code == 404          # ответа уже нет
    items = await _day_items(anon, scr)
    assert items[i1]["owner_status"] == "pending" and items[i1]["response"] is None
    log = await pool.fetch("SELECT action, old_answer, new_answer FROM kpi_downtime_responses_log WHERE action = 'delete' ORDER BY id")
    assert [(r["old_answer"], r["new_answer"]) for r in log] == [("agree", None), ("agree", None)]


async def test_downtimes_list_has_response(app, users, anon, pool):
    scr, dt, (iid,) = await _seed_items(pool, users, ("ito",), (6,))
    await users["t_admin"].put(_url(iid), json={"answer": "disagree", "comment": "проверить"})
    r = await anon.get("/api/kpi/downtimes-list", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    row = next(x for x in r.json()["rows"] if x["kind"] == "item")
    assert row["owner_status"] == "disputed" and row["response"]["answer"] == "disagree" and row["response"]["comment"] == "проверить"
    assert r.json()["responsibility"]["disputed"] == 6


# ---------- Excel = экран ----------

async def test_excel_shows_statuses_and_new_minutes(app, users, anon, pool):
    scr, dt, (i1, i2, i3) = await _seed_items(pool, users, ("oto", "ito", "oto"), (10, 6, 4))
    await users["t_oto"].put(_url(i1), json={"answer": "partial", "minutes": 4})
    await users["t_admin"].put(_url(i2), json={"answer": "disagree", "comment": "не наше"})
    t = await _resp(anon, scr)
    assert t["owners"]["oto"] == 8 and t["owners"]["production"] == 6 and t["owners"]["ito"] == 6
    assert t["pending"] == 4 and t["disputed"] == 6
    pv = (await anon.get(f"/api/kpi/screens/{scr}/day-preview", params={"date": DAY.isoformat()})).json()
    c = pv["headers"].index("Отв.")
    assert [r[c] for r in pv["rows"]][:3] == ["ОТО ✓", "ИТО !", "ОТО …"]
    assert pv["responsibility"] == t
    for url, params in (("day.xlsx", {"date": DAY.isoformat(), "src": "andon"}),
                        ("period.xlsx", {"from": DAY.isoformat(), "to": DAY.isoformat(), "src": "reports"})):
        xl = await anon.get(f"/api/kpi/screens/{scr}/{url}", params=params)
        assert xl.status_code == 200
        _, mins, _ = _block(xl.content)
        assert mins[1:] == [t["total"], t["owners"]["production"], t["owners"]["oto"], t["owners"]["ito"], t["owners"]["other"],
                            t["owners"]["none"], t["undescribed"], t["pending"], t["disputed"]], url
        s = zipfile.ZipFile(io.BytesIO(xl.content)).read("xl/sharedStrings.xml").decode("utf-8")
        assert "ОТО ✓" in s and "ИТО !" in s and "ОТО …" in s, url


# ---------- экран: исходники и чистая логика окна (node) ----------

def _src(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_page_loads_dialog_script():
    s = _src("index.html")
    assert 'src="/static/dtresponse.js"' in s and s.index("/static/downtime.js") < s.index("/static/dtresponse.js")
    assert "dtResponse" in _src("downtime.js") and "dtResponse" in _src("downtimes.js")
    js = _src("dtresponse.js")
    for text in ("Ответ отдела", "Согласен", "Частично", "Не согласен", "Сохранить", "Отозвать ответ", "Отмена", "Escape"):
        assert text in js, text
    assert "AbortSignal.timeout(6000)" in js
    assert ".dr-box" in _src("downtime.css")


def run(expr):
    code = f"const r = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True, encoding="utf-8").stdout)


OTO = {"id": 5, "role": "maintenance", "department": "oto"}


@node
def test_js_rights_like_server():
    assert run(f"r.canRespond({json.dumps(OTO)}, 'oto')") is True
    assert run(f"r.canRespond({json.dumps(OTO)}, 'ito')") is False
    assert run(f"r.canRespond({json.dumps(OTO)}, 'production')") is False
    assert run("r.canRespond({id: 1, role: 'viewer', department: 'oto'}, 'oto')") is False
    assert run("r.canRespond({id: 1, role: 'chief', department: 'production'}, 'ito')") is True
    assert run("r.canRespond({id: 1, role: 'admin', department: null}, 'oto')") is True
    assert run("r.canRespond(null, 'oto')") is False                               # табло без входа ничего не кликает
    assert run(f"r.canWithdraw({json.dumps(OTO)}, {{user_id: 5}})") is True
    assert run("r.canWithdraw({id: 2, role: 'chief'}, {user_id: 5})") is False
    assert run("r.canWithdraw({id: 2, role: 'admin'}, {user_id: 5})") is True
    assert run(f"r.canWithdraw({json.dumps(OTO)}, null)") is False


@node
def test_js_rest_and_validation():
    assert run("r.restHint(10, 3)") == "Остаток 7 мин уйдёт на ПР"
    assert run("r.restHint(10, 2.5)") == "Остаток 7,5 мин уйдёт на ПР"
    assert run("r.restHint(10, 0)") == "Укажите от 0,1 до 9,9 мин"
    assert run("r.validate('partial', 10, 10, '')") != ""
    assert run("r.validate('partial', 10, 0, '')") != ""
    assert run("r.validate('partial', 10, 4, '')") == ""
    assert run("r.validate('disagree', 10, null, '  ')") == "Не согласен: напишите комментарий, почему"
    assert run("r.validate('disagree', 10, null, 'не наше')") == ""
    assert run("r.validate('', 10, null, '')") == "Выберите ответ"
    assert run("r.validate('agree', 10, null, '')") == ""


@node
def test_js_labels_and_title():
    assert run("r.dialogTitle('oto')") == "Ответ отдела ОТО"
    assert run("r.dialogTitle('ito')") == "Ответ отдела ИТО"
    resp = {"answer": "disagree", "comment": "не наше", "author": "Иванов, ТО", "updated_at": "2026-10-19T08:30:00+05:00"}
    tip = run(f"r.ownTip('oto', 'disputed', {json.dumps(resp)}, 'Оборудование')")
    for part in ("ОТО", "спорно", "Иванов, ТО", "не наше", "Оборудование"):
        assert part in tip, part
    assert "ждёт подтверждения" in run("r.ownTip('ito', 'pending', null, '')")
    partial = {"answer": "partial", "minutes": 3, "author": "Петров, ТО", "updated_at": "2026-10-19T08:30:00+05:00"}
    assert "3 мин" in run(f"r.ownTip('oto', 'confirmed', {json.dumps(partial)}, '', 10)")
    assert "7 мин" in run(f"r.ownTip('oto', 'confirmed', {json.dumps(partial)}, '', 10)")      # остаток на ПР
    # «содержание, затем автор»: «Ответил(а) Фамилия, должность, дата время»
    line = run(f"r.authorLine({json.dumps(resp)})")
    assert line.startswith("Ответил(а) Иванов, ТО, ") and "19.10" in line
    assert run("r.authorLine(null)") == ""
