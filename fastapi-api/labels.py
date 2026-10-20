"""Словарь названий интерфейса («Названия»): администратор меняет подписи и подсказки, экран и Excel берут их из базы.

Главное правило: стандартные названия остаются в коде. Пустая таблица или недоступная база означают, что сайт и Excel выглядят как раньше.
- GET /api/ui-labels читают все без входа (отдаются только изменённые подписи);
- PUT /api/ui-labels/{key} и DELETE /api/ui-labels/{key} (сброс) только администратор, название не длиннее 40 символов;
- label(key, default) для Excel и серверных текстов: кэш 5 секунд, при ошибке базы остаётся стандартное название.
"""
from __future__ import annotations

import time
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import require_admin

router = APIRouter(prefix="/api")

MAX_TEXT = 40       # название длиннее не помещается в шапку на 955 px
MAX_TITLE = 300
CACHE_TTL = 5.0     # секунд: Excel и серверные подписи перечитывают словарь не чаще

# Каталог подписей первого набора. text — стандартное название (оно же записано в коде экрана и Excel), title — стандартная подсказка
# ("" — у подписи нет собственной подсказки). where — где подпись видна. Новая подпись сначала заводится здесь.
_DOWN_HINT = ("Отставание от плана, мин: (план − факт) ÷ план × длительность интервала; включает остановки и медленную работу")
CATALOG: list[dict[str, str]] = [
    {"key": "down_min", "where": "Andon, Простои, Отчёты, Графики и Excel: колонка и плитка", "text": "Отставание, мин", "title": _DOWN_HINT},
    {"key": "col_down_report", "where": "Отчёты и Excel: заголовок колонки в таблице отчёта дня", "text": "Отставание мин", "title": _DOWN_HINT},
    {"key": "chart_down_days", "where": "Графики, Excel: график по дням", "text": "Простой по дням, мин",
     "title": "Сколько минут линия простаивала в каждый день (отставание от плана)"},
    {"key": "chart_down_stations", "where": "Графики, Excel: график по станциям", "text": "Простой по станциям (топ-15)",
     "title": "Станции с наибольшими минутами простоя за период, от самых долгих; показаны первые пятнадцать станций линии"},
    {"key": "table_down_stations", "where": "Отчёты, Excel: таблица по станциям", "text": "Простой по станциям",
     "title": "Минуты простоя по станциям за период, от самых долгих, с долей каждой станции от общей суммы"},
    {"key": "chart_tip_down", "where": "Графики: подсказка над столбцом дня («простой N мин»)", "text": "простой", "title": ""},
    {"key": "dt_window_word", "where": "Окно описания: заголовок «Отставание N мин — линия»", "text": "Отставание", "title": ""},
    {"key": "tab_downtimes", "where": "Вкладка «Простои»", "text": "Простои",
     "title": "Все простои за период одним списком: по одной строке на каждую причину, с описаниями и комментариями"},
    {"key": "tab_charts", "where": "Вкладка «Графики»", "text": "Графики",
     "title": "Графики по линии за период: план и факт, простои, причины и станции; каждый график можно выгрузить в Excel"},
    {"key": "tab_reports", "where": "Вкладка «Отчёты»", "text": "Отчёты",
     "title": "Отчёт дня по линии: график план/факт и таблица как в файле Excel, выбор дня и скачивание; "
              "администратор здесь же настраивает автосохранение отчётов в папку"},
    {"key": "tab_trends", "where": "Вкладка «Тренды»", "text": "Тренды",
     "title": "Технические графики за период: связь с ПЛК (когда связь была и когда пропадала), выгрузка в Excel"},
    {"key": "tab_notifications", "where": "Вкладка «Оповещения»", "text": "Оповещения",
     "title": "Параметры подключения к ПЛК и настройки оповещений"},
    {"key": "tab_dictionary", "where": "Вкладка «Справочники»", "text": "Справочники",
     "title": "Справочники простоев: причины, участки и станции для описания простоев (редактируют администратор и начальники)"},
    {"key": "tab_users", "where": "Вкладка «Пользователи»", "text": "Пользователи",
     "title": "Пользователи системы: создание учётных записей, роли, сброс паролей (только для администратора)"},
    {"key": "tile_days", "where": "Графики, Отчёты, Excel: плитка", "text": "Дней с данными",
     "title": "Сколько дней периода с выпуском; дни без выпуска (на экране «Отчёты» серые строки) не считаются"},
    {"key": "tile_plan", "where": "Графики, Отчёты, Excel: плитка", "text": "План",
     "title": "Плановое количество кузовов за выбранный период, без дней без выпуска"},
    {"key": "tile_fact", "where": "Графики, Отчёты, Excel: плитка", "text": "Факт",
     "title": "Фактическое количество кузовов за выбранный период, без дней без выпуска"},
    {"key": "tile_exec", "where": "Графики, Отчёты, Excel: плитка", "text": "Выполнение",
     "title": "Выполнение плана в процентах: факт делённый на план, без дней без выпуска"},
    {"key": "tile_undescribed", "where": "Графики, Отчёты, Excel: плитка", "text": "Не описано, мин",
     "title": "Минуты отставания, для которых ещё не указана причина; их нужно описать в окне простоя"},
    {"key": "chart_plan_fact", "where": "Графики, Отчёты, Excel: график по дням", "text": "План, факт и % выполнения по дням",
     "title": "Столбцы: план и факт кузовов за день; линия: процент выполнения плана (правая шкала)"},
    {"key": "chart_plan_fact_hour", "where": "Отчёты, Excel: график дня", "text": "План и факт по часам",
     "title": "Плановое и фактическое количество кузовов по часовым интервалам дня; линия показывает % выполнения"},
    {"key": "chart_pareto", "where": "Графики, Excel: график Парето", "text": "Парето причин простоя (топ-10)",
     "title": "Причины по убыванию минут простоя; линия показывает накопленную долю: сколько простоя дают первые причины"},
    {"key": "chart_cumulative", "where": "Графики, Excel: график за день", "text": "Накопленное производство за день",
     "title": "Линии: накопленные план и факт по интервалам смены; столбцы: разница факта и плана (ниже нуля: отстаём от плана)"},
    {"key": "card_by_days", "where": "Отчёты, Excel: таблица по дням", "text": "По дням",
     "title": "Итоги по каждому дню периода: план, факт, выполнение и отставание"},
    {"key": "card_reasons", "where": "Отчёты, Excel: таблица причин", "text": "Причины простоя",
     "title": "Минуты простоя по причинам за период, от самых долгих; доля каждой причины от общей суммы"},
]
BY_KEY = {c["key"]: c for c in CATALOG}

_cache: dict[str, Any] = {"data": {}, "at": 0.0}   # key -> {"text": str, "title": str}; непустые поля только у изменённых подписей


def reset_cache() -> None:
    _cache["data"] = {}
    _cache["at"] = 0.0


async def ensure_labels_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_labels (
            key             TEXT PRIMARY KEY,
            text            TEXT NOT NULL DEFAULT '',
            title           TEXT NOT NULL DEFAULT '',
            updated_by_name TEXT NOT NULL DEFAULT '',
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_labels_log (
            id         SERIAL PRIMARY KEY,
            key        TEXT        NOT NULL,
            was_text   TEXT        NOT NULL DEFAULT '',
            now_text   TEXT        NOT NULL DEFAULT '',
            was_title  TEXT        NOT NULL DEFAULT '',
            now_title  TEXT        NOT NULL DEFAULT '',
            by_name    TEXT        NOT NULL DEFAULT '',
            at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def refresh(pool: asyncpg.Pool, force: bool = False) -> None:
    """Перечитывает изменённые подписи не чаще раза в CACHE_TTL секунд; при ошибке базы остаются прежние (или стандартные)."""
    now = time.monotonic()
    if not force and now - _cache["at"] < CACHE_TTL:
        return
    try:
        rows = await pool.fetch("SELECT key, text, title FROM ui_labels WHERE text <> '' OR title <> ''")
    except Exception:   # таблицы ещё нет, база недоступна или тестовая копия: действуют стандартные названия
        _cache["at"] = now
        return
    _cache["data"] = {r["key"]: {"text": r["text"], "title": r["title"]} for r in rows}
    _cache["at"] = now


def label(key: str, default: str | None = None) -> str:
    """Название по ключу: изменённое администратором или стандартное (default, иначе стандартное из каталога)."""
    std = default if default is not None else (BY_KEY.get(key) or {}).get("text", "")
    custom = (_cache["data"].get(key) or {}).get("text") or ""
    return custom or std


def label_title(key: str, default: str | None = None) -> str:
    std = default if default is not None else (BY_KEY.get(key) or {}).get("title", "")
    return (_cache["data"].get(key) or {}).get("title") or std


class LabelBody(BaseModel):
    text: str = Field(default="", max_length=MAX_TEXT)
    title: str = Field(default="", max_length=MAX_TITLE)


def _key(key: str) -> str:
    if key not in BY_KEY:
        raise HTTPException(status_code=404, detail="Такой подписи нет в словаре")
    return key


@router.get("/ui-labels", summary="Изменённые названия интерфейса (читают все без входа)")
async def get_labels(request: Request) -> dict[str, Any]:
    await refresh(request.app.state.pool, force=True)
    return {"labels": {k: dict(v) for k, v in _cache["data"].items() if k in BY_KEY}}


@router.get("/ui-labels/catalog", summary="Словарь названий для окна «Названия» (только администратор)")
async def get_catalog(request: Request, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch("SELECT key, text, title, updated_by_name, updated_at FROM ui_labels")
    cur = {r["key"]: r for r in rows}
    out = []
    for c in CATALOG:
        r = cur.get(c["key"])
        out.append({**c, "custom_text": r["text"] if r else "", "custom_title": r["title"] if r else "",
                    "updated_by_name": r["updated_by_name"] if r else "",
                    "updated_at": r["updated_at"].isoformat() if r and (r["text"] or r["title"]) else None})
    return {"items": out, "max_text": MAX_TEXT}


async def _save(pool: asyncpg.Pool, key: str, text: str, title: str, who: str) -> None:
    async with pool.acquire() as con, con.transaction():
        was = await con.fetchrow("SELECT text, title FROM ui_labels WHERE key = $1 FOR UPDATE", key)
        was_text, was_title = (was["text"], was["title"]) if was else ("", "")
        if (was_text, was_title) == (text, title):
            return
        if text or title:
            await con.execute(
                """
                INSERT INTO ui_labels (key, text, title, updated_by_name, updated_at) VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (key) DO UPDATE SET text = EXCLUDED.text, title = EXCLUDED.title,
                    updated_by_name = EXCLUDED.updated_by_name, updated_at = now()
                """,
                key, text, title, who,
            )
        else:
            await con.execute("DELETE FROM ui_labels WHERE key = $1", key)
        await con.execute(
            "INSERT INTO ui_labels_log (key, was_text, now_text, was_title, now_title, by_name) VALUES ($1, $2, $3, $4, $5, $6)",
            key, was_text, text, was_title, title, who,
        )
    await refresh(pool, force=True)   # правка видна в Excel сразу, а не через 5 секунд


@router.put("/ui-labels/{key}", summary="Задать своё название и подсказку (только администратор)")
async def put_label(key: str, body: LabelBody, request: Request, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    key = _key(key)
    text, title = body.text.strip(), body.title.strip()
    if "\n" in text or "<" in text or ">" in text:
        raise HTTPException(status_code=422, detail="В названии нельзя переводить строку и использовать знаки < >")
    await _save(request.app.state.pool, key, text, title, user.get("full_name") or user.get("login") or "")
    return {"status": "ok", "key": key, "text": text, "title": title}


@router.delete("/ui-labels/{key}", summary="Сбросить название и подсказку на стандартные (только администратор)")
async def reset_label(key: str, request: Request, user: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    key = _key(key)
    await _save(request.app.state.pool, key, "", "", user.get("full_name") or user.get("login") or "")
    return {"status": "ok", "key": key}
