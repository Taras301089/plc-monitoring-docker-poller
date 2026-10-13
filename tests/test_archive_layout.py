"""Режим «архив» на экране Andon: надпись и кнопки не перекрываются, значок календаря в полях даты заметен в обеих темах."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = "\n".join((STATIC / n).read_text(encoding="utf-8") for n in ("archive.js", "downtime.css"))


def rules(selector_part):
    """Правила CSS (текст между { }) для селекторов, содержащих заданную подстроку."""
    return [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", HTML) if selector_part in m.group(1)]


def test_message_state_puts_bar_in_flow():
    css = [r for r in rules("#andon.andon-msg .andon-top")]
    assert css, "нет правила для панели при сообщении"
    assert all("position: static" in r and "flex-wrap: wrap" in r and "absolute" not in r for r in css)
    assert rules("#andon.andon-msg .andon-day") and "flex-wrap: wrap" in rules("#andon.andon-msg .andon-day")[0]


def test_message_class_is_toggled_by_script():
    msg = HTML[HTML.index("function showAndonMessage"):HTML.index("function showAndonMessage") + 400]
    assert "classList.add('andon-msg')" in msg
    for fn in ("function buildKpiLayout", "function buildFlLayout"):
        assert "classList.remove('andon-msg')" in HTML[HTML.index(fn):HTML.index(fn) + 160], fn


def test_archive_day_group_has_no_absolute_positioning():
    day = [r for r in re.findall(r"\.andon-day[^{}]*\{([^{}]*)\}", HTML + (STATIC / "downtime.css").read_text(encoding="utf-8"))]
    assert day and not any("absolute" in r or "fixed" in r for r in day)


def test_date_picker_icon_visible_in_both_themes():
    ind = rules('input[type="date"]::-webkit-calendar-picker-indicator')
    assert ind and "cursor: pointer" in ind[0] and "filter: var(--date-icon-filter)" in ind[0]
    dark = HTML[HTML.index(":root {"):HTML.index("[data-theme=\"light\"] {")]
    light = HTML[HTML.index("[data-theme=\"light\"] {"):HTML.index("[data-theme=\"light\"] {") + 1500]
    assert "--date-icon-filter: invert(1)" in dark          # тёмная тема: значок светлый
    assert "--date-icon-filter: none" in light              # светлая тема: значок тёмный
    assert "width: 1.25em" in ind[0]                        # крупнее стандартного


def test_no_per_screen_date_icon_overrides():
    """Одно общее правило: в js и css экранов нет своих правил для значка календаря."""
    assert "calendar-picker-indicator" not in JS
