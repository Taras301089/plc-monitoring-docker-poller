"""Итоги отставания по отделам: ПР / ОТО / ИТО / ДР, «не описано», «ждёт подтверждения», «спорно».

Одна функция расчёта для Andon, вкладки «Простои», «Отчётов» и Excel. Всё отставание берётся из общего расчёта (period.summarize поверх
period._load и правил dayflags), поэтому число совпадает с плиткой «Простой, мин»: дни без выпуска не входят, остаются только описанные людьми минуты.
Сумма ПР + ОТО + ИТО + ДР + «не описано» всегда равна всему отставанию; «ждёт подтверждения» и «спорно» это срез внутри отделов, а не слагаемые."""
from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Request

import downtime

router = APIRouter(prefix="/api/kpi")

OWNER_KEYS = ("production", "oto", "ito", "other")
TITLE = "По ответственным"
# столбцы блока на экране и в Excel (в том же порядке и с теми же названиями, что HEADS в responsibility.js)
HEADS = ("", "Всего", "ПР", "ОТО", "ИТО", "ДР", "Не описано", "Ждёт подтверждения", "Спорно")
ROW_MINUTES = "Минут"
ROW_SHARE = "Доля, %"
MAX_DAYS = 92


def _zero() -> dict[str, float]:
    return {k: 0.0 for k in OWNER_KEYS}


def owner_split(data: dict[str, Any]) -> dict[str, Any]:
    """Итоги по отделам из данных period._load. Отставание берётся из summarize (то же число, что в плитках), описанные минуты дня
    не превышают его (при превышении на минуту лишнее отсекается), остаток дня это «не описано»."""
    from period import summarize   # period импортирует этот модуль для Excel, поэтому подключаем здесь

    days = data["days"]
    if not days and data["dt_minutes"]:
        # у экрана подсборки своей почасовой истории нет, но его простои считаются: дни берём из самих простоев
        days = [{"prod_date": d, "plan": 0, "fact": 0} for d in sorted(data["dt_minutes"])]
    sm = summarize({**data, "days": days})
    left = {r["date"]: r["downtime"] for r in sm["days"]}
    owners, pending_by, disputed_by = _zero(), _zero(), _zero()
    for it in data["items"]:
        day = it["prod_date"]
        m = min(float(it["minutes"]), left.get(day, 0.0))
        if m <= 0:
            continue
        left[day] = round(left[day] - m, 6)
        o = it["owner"] if it["owner"] in OWNER_KEYS else "production"
        owners[o] += m
        st = downtime.owner_status(o)
        if st == "pending":
            pending_by[o] += m
        elif st == "disputed":
            disputed_by[o] += m
    total = round(sm["totals"]["downtime"], 1)
    owners = {k: round(v, 1) for k, v in owners.items()}
    undescribed = max(0.0, round(total - sum(owners.values()), 1))
    return _pack(total, owners, undescribed, pending_by, disputed_by)


def _pack(total: float, owners: dict[str, float], undescribed: float, pending_by: dict[str, float], disputed_by: dict[str, float]) -> dict[str, Any]:
    pending_by = {k: round(v, 1) for k, v in pending_by.items()}
    disputed_by = {k: round(v, 1) for k, v in disputed_by.items()}

    def share(v: float) -> float:
        return round(v / total * 100, 1) if total else 0.0

    return {
        "total": total, "owners": owners, "undescribed": undescribed,
        "pending": round(sum(pending_by.values()), 1), "disputed": round(sum(disputed_by.values()), 1),
        "pending_by": pending_by, "disputed_by": disputed_by,
        "share": {**{k: share(v) for k, v in owners.items()}, "undescribed": share(undescribed),
                  "pending": share(sum(pending_by.values())), "disputed": share(sum(disputed_by.values()))},
    }


async def responsibility_totals(pool: asyncpg.Pool, screen_id: int, d_from: date, d_to: date) -> dict[str, Any]:
    """Итоги отставания по отделам одного экрана за период (день: d_from == d_to)."""
    from period import _load

    return owner_split(await _load(pool, screen_id, d_from, d_to))


async def responsibility_all(pool: asyncpg.Pool, d_from: date, d_to: date) -> dict[str, Any]:
    """То же по всем экранам с простоями за период (вкладка «Простои» показывает все линии сразу)."""
    ids = [r["screen_id"] for r in await pool.fetch(
        "SELECT DISTINCT screen_id FROM kpi_downtimes WHERE prod_date BETWEEN $1 AND $2 AND source = 'plan'", d_from, d_to)]
    total, undescribed = 0.0, 0.0
    owners, pending_by, disputed_by = _zero(), _zero(), _zero()
    for sid in ids:
        r = await responsibility_totals(pool, sid, d_from, d_to)
        total += r["total"]
        undescribed += r["undescribed"]
        for k in OWNER_KEYS:
            owners[k] += r["owners"][k]
            pending_by[k] += r["pending_by"][k]
            disputed_by[k] += r["disputed_by"][k]
    return _pack(round(total, 1), {k: round(v, 1) for k, v in owners.items()}, round(undescribed, 1), pending_by, disputed_by)


@router.get("/screens/{screen_id}/responsibility")
async def get_responsibility(
    screen_id: int, request: Request,
    date_from: date = Query(alias="from"), date_to: date = Query(alias="to"),
) -> dict[str, Any]:
    """Итоги отставания по отделам за день или период; чтение без входа, как у соседних данных."""
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="Дата «по» раньше даты «с»")
    if (date_to - date_from).days > MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Период не больше {MAX_DAYS} дней")
    pool: asyncpg.Pool = request.app.state.pool
    if await pool.fetchval("SELECT 1 FROM kpi_screens WHERE id = $1", screen_id) is None:
        raise HTTPException(status_code=404, detail="Экран не найден")
    return await responsibility_totals(pool, screen_id, date_from, date_to)


def write_responsibility(wb: xlsxwriter.Workbook, ws: Any, r0: int, resp: dict[str, Any]) -> int:
    """Блок «По ответственным» в Excel с строки r0 (как на экране): заголовок, шапка, строка минут («Всего» формулой суммы отделов и «не описано»)
    и строка долей. Возвращает первую свободную строку под блоком."""
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter"})
    section = wb.add_format({"bold": True, "font_size": 12})
    n1 = wb.add_format({"num_format": "0.0"})
    bn1 = wb.add_format({"num_format": "0.0", "bold": True})
    pct = wb.add_format({"num_format": "0.0%"})
    lab = wb.add_format({"bold": True})
    o, total = resp["owners"], resp["total"]
    ws.write(r0, 0, TITLE, section)
    ws.write_row(r0 + 1, 0, list(HEADS), head)
    ws.write(r0 + 2, 0, ROW_MINUTES, lab)
    ws.write_formula(r0 + 2, 1, "=" + "+".join(f"{c}{r0 + 3}" for c in "CDEFG"), bn1, total)   # сумма отделов и «не описано» (не SUM: это не столбец таблицы)
    for c, v in enumerate([o["production"], o["oto"], o["ito"], o["other"], resp["undescribed"], resp["pending"], resp["disputed"]], start=2):
        ws.write_number(r0 + 2, c, v, n1)
    ws.write(r0 + 3, 0, ROW_SHARE, lab)
    sh = resp["share"]
    for c, key in enumerate(["total", "production", "oto", "ito", "other", "undescribed", "pending", "disputed"], start=1):
        letter = xlsxwriter.utility.xl_col_to_name(c)
        cached = (100.0 if total else 0.0) if key == "total" else sh[key]
        ws.write_formula(r0 + 3, c, f'=IF($B{r0 + 3}=0,"",{letter}{r0 + 3}/$B{r0 + 3})', pct, cached / 100 if total else 0)   # при нуле Excel сам покажет пусто (формула), в кэше число, чтобы читалки без пересчёта не спотыкались
    return r0 + 5
