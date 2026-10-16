"""Вкладка «Графики»: запрос накопленного графика с тайм-аутом 6 с (правило «Идентичность данных», п. 4).

Зависший запрос не должен останавливать обновление вкладки; при обрыве график остаётся прежним."""
import re
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "charts.js").read_text(encoding="utf-8")


def _func(name):
    m = re.search(r"async function " + name + r"\(.*?\n  \}\n", JS, re.S)
    assert m, name
    return m.group(0)


def test_cumulative_fetch_has_6s_timeout():
    body = _func("loadCumulative")
    assert re.search(r"fetch\(`/api/kpi/charts/cumulative[^`]*`,\s*\{\s*signal:\s*AbortSignal\.timeout\(6000\)", body)


def test_cumulative_timeout_message_in_russian():
    assert "TimeoutError" in _func("loadCumulative")


def test_cumulative_kept_on_failure():
    # прежний график не сбрасывается перед запросом: при обрыве он остаётся на экране
    assert "cum = null" not in _func("loadCumulative")
    assert "cum = null" not in _func("load")
    # показывается только график той же линии и того же дня
    assert re.search(r"cum\.screen === state\.screen", JS)
