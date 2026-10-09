"""Andon: «ИТОГО ±» это сумма «±» закрытых интервалов (решение пользователя 09.10: живой расчёт «факт минус весь план дня» вводил в заблуждение)."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "index.html").read_text(encoding="utf-8")


def _block():
    i = HTML.index("const ds = q('deltaSum');")
    return HTML[i - 200: i + 500]


def test_total_delta_is_sum_of_closed_intervals_delta():
    assert "deltaSum" in _block() and "hasDelta" in _block()
    assert "tot - planSum" not in _block()               # не «факт минус план всего дня»


def test_interval_delta_only_for_closed_intervals():
    assert "idx && i + 1 < idx && (cfg.plan || fact)" in HTML
    i = HTML.index("idx && i + 1 < idx && (cfg.plan || fact)")
    assert "deltaSum += d; hasDelta = true;" in HTML[i: i + 200]
