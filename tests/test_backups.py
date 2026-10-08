"""Раздел «Бэкапы»: доступ только администратору, список дампов, прогноз места на диске."""
import json
from datetime import date

import pytest

GB = 1024**3


def _forecast():
    import backups
    return backups.forecast


def test_forecast_no_data_with_short_history():
    r = _forecast()([(date(2026, 10, 1), 3_000_000), (date(2026, 10, 4), 3_100_000)], 50_000_000, 100 * GB)
    assert r["status"] == "no_data" and r["days_left"] is None


def test_forecast_days_left_from_growth():
    # дамп вырос на 10 МБ за 10 дней, база в 10 раз больше дампа: рост базы 10 МБ/день
    pts = [(date(2026, 10, 1), 100_000_000), (date(2026, 10, 11), 110_000_000)]
    r = _forecast()(pts, db_size=1_100_000_000, disk_free=10_000_000_000)
    assert r["status"] == "ok"
    assert r["growth_per_day"] == 10_000_000
    assert r["days_left"] == 1000


def test_forecast_no_growth():
    pts = [(date(2026, 10, 1), 100_000_000), (date(2026, 10, 11), 90_000_000)]
    r = _forecast()(pts, db_size=900_000_000, disk_free=10 * GB)
    assert r["status"] == "no_growth" and r["days_left"] is None


async def test_backups_only_for_admin(users, anon):
    assert (await anon.get("/api/backups")).status_code == 401
    for login in ("t_chief", "t_area", "t_master", "t_oto", "t_viewer"):
        assert (await users[login].get("/api/backups")).status_code == 403, login
    assert (await users["t_admin"].get("/api/backups")).status_code == 200


async def test_backups_reports_files_and_status(users, tmp_path, monkeypatch):
    import backups

    (tmp_path / "hub_2026-10-07.dump").write_bytes(b"x" * 2048)
    (tmp_path / "n8n_2026-10-07.dump").write_bytes(b"x" * 1024)
    (tmp_path / "status.json").write_text(
        json.dumps({"ok": True, "error": "", "keep_days": 30, "disk_total": 100 * GB, "disk_free": 40 * GB}),
        encoding="utf-8",
    )
    monkeypatch.setattr(backups, "BACKUP_DIR", tmp_path)

    data = (await users["t_admin"].get("/api/backups")).json()
    assert data["folder_available"] is True
    assert {f["name"] for f in data["files"]} == {"hub_2026-10-07.dump", "n8n_2026-10-07.dump"}
    assert data["backups_total"] == 3072
    assert data["status_ok"] is True and data["keep_days"] == 30
    assert data["disk_free"] == 40 * GB
    assert data["last_backup_age_hours"] is not None and data["last_backup_age_hours"] < 1
    assert data["db_size"] > 0 and len(data["tables"]) > 0
    assert data["forecast"]["status"] == "no_data"


async def test_backups_folder_missing(users, tmp_path, monkeypatch):
    import backups

    monkeypatch.setattr(backups, "BACKUP_DIR", tmp_path / "nope")
    data = (await users["t_admin"].get("/api/backups")).json()
    assert data["folder_available"] is False
    assert data["files"] == [] and data["last_backup_age_hours"] is None
