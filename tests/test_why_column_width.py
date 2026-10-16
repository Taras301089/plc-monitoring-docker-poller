"""Колонка «Причина» на Andon: одна причина занимает всю свободную ширину ячейки, а не 48%."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
CSS = (STATIC / "downtime.css").read_text(encoding="utf-8")
JS = (STATIC / "downtime.js").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, selector
    return m.group(1)


def test_single_reason_is_not_capped_at_half_width():
    body = _rule(".kpi-table td.why .why-col")
    assert "48%" not in body
    assert "max-width: 100%" in body


def test_fit_hides_columns_only_when_text_is_truncated():
    fit = JS[JS.index("function fitWhy"): JS.index("window.addEventListener('resize'")]
    assert "scrollWidth" in fit and "clientWidth" in fit
    assert "i >= 1" in fit  # первая колонка остаётся всегда
