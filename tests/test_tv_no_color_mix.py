"""Андон должен выглядеть правильно на старом браузере телевизора: в стилях экрана нет «голого» color-mix().

На телевизоре (Яндекс-браузер для ТВ, старый Chromium) функции color-mix() нет: браузер отбрасывает всё объявление, и пропадает, например,
заливка строки текущего часа (19.10.2026: на табло FL была видна только тонкая полоска слева). Поэтому в правилах экрана Андон
(таблица .kpi-table, значок .live-badge, строки .andon-*, меню .am-*) каждое объявление с color-mix() должно иметь запасное
объявление того же свойства в этом же правиле ВЫШЕ (обычный цвет или готовый rgba из палитры), либо вообще обходиться без color-mix()."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")

# селекторы экрана Андон; новые части экрана добавляются сюда
ANDON_MARKERS = (".kpi-table", ".live-badge", ".andon", ".am-", "#andon", ".plan-")
RULE = re.compile(r"([^{}@;]+)\{([^{}]*)\}")


def _declarations(body: str):
    for d in body.split(";"):
        if ":" in d:
            prop, value = d.split(":", 1)
            yield prop.strip().lower(), value.strip()


def _style_text() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", INDEX, flags=re.S))


def _violations() -> list[str]:
    bad = []
    for m in RULE.finditer(_style_text()):
        selector, body = " ".join(m.group(1).split()), m.group(2)
        if not any(k in selector for k in ANDON_MARKERS) or "@keyframes" in selector:
            continue
        seen_plain: set[str] = set()
        for prop, value in _declarations(body):
            if "color-mix(" in value:
                if prop not in seen_plain:
                    bad.append(f"{selector} {{ {prop}: {value[:70]} }}")
            else:
                seen_plain.add(prop)
    return bad


def test_andon_styles_have_fallback_for_color_mix():
    bad = _violations()
    assert not bad, "color-mix() без запасного значения в стилях Андон (на телевизоре объявление пропадёт):\n" + "\n".join(bad)


def test_current_hour_row_uses_plain_color():
    """Строка текущего часа (tr.now): заливка обычным цветом из палитры, не color-mix()."""
    m = re.search(r"\.kpi-table tr\.now\s*\{([^}]*)\}", INDEX)
    assert m and "color-mix(" not in m.group(1) and "var(--ok-a18)" in m.group(1)
    assert "--ok-a18:" in INDEX      # цвет определён в палитре тёмной и светлой темы
    assert INDEX.count("--ok-a18:") >= 2
