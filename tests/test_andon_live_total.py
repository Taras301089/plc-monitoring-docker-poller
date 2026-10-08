"""Andon: «ИТОГО ±» считается живым: итоговый факт минус итоговый план, не дожидаясь закрытия текущего интервала."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def _block():
    i = HTML.index("const ds = q('deltaSum');")
    return HTML[i: i + 500]


def test_total_delta_is_live_fact_minus_plan():
    assert "tot - planSum" in _block()


def test_total_delta_does_not_wait_for_closed_intervals():
    assert "hasDelta" not in _block()


def test_interval_delta_still_only_for_closed_intervals():
    """Колонка «±» по строкам остаётся как есть: считается после закрытия интервала (решение пользователя)."""
    assert "idx && i + 1 < idx && (cfg.plan || fact)" in HTML
