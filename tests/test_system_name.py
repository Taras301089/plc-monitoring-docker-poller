"""Название системы: PMM (Production Monitoring & Management) в заголовках вкладок и справке."""
from pathlib import Path

ST = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
INDEX = (ST / "index.html").read_text(encoding="utf-8")
REF = (ST / "reference.html").read_text(encoding="utf-8")


def test_browser_tab_titles():
    assert "<title>PMM — Production Monitoring &amp; Management</title>" in INDEX
    assert "<title>Справка — PMM</title>" in REF


def test_reference_intro_names_the_system():
    assert "PMM (Production Monitoring &amp; Management)" in REF
    assert "система мониторинга и управления производством" in REF
