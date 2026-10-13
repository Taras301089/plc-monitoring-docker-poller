"""Журнал изменений справки: одна запись на строку (для построчного слияния Git), даты по убыванию, текст не потерян."""
import re
from pathlib import Path

REF_PATH = Path(__file__).resolve().parent.parent / "fastapi-api" / "static" / "reference.html"
REF = REF_PATH.read_text(encoding="utf-8")
LI = re.compile(r'^\s*<li data-date="(\d{4}-\d{2}-\d{2})">(.*)</li>\s*$')


def entries():
    out = []
    inside = False
    for line in REF.splitlines():
        if re.search(r'<ul id="changelog(-old)?">', line):
            inside = True
            continue
        if inside and line.strip() == "</ul>":
            inside = False
            continue
        if inside:
            m = LI.match(line)
            assert m, f"запись журнала должна лежать на одной строке: {line[:80]}"
            out.append((m.group(1), m.group(2)))
    return out


def test_changelog_exists_and_one_entry_per_line():
    assert 'id="changelog"' in REF and "Более ранние изменения" in REF
    e = entries()
    assert len(e) >= 20
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e[0][0])


def test_dates_descending():
    dates = [d for d, _ in entries()]
    assert dates == sorted(dates, reverse=True)


def test_only_latest_three_dates_visible():
    top = REF.split('<details class="changelog-old">')[0]
    shown = set(re.findall(r'<li data-date="([\d-]+)"', top))
    assert len(shown) <= 3


def test_no_text_lost():
    text = " ".join(t for _, t in entries())
    for phrase in ["Takt Time", "Простой RB262", "дни без выпуска", "Графики переменных", "режим «Аналитика»",
                   "время ПЛК", "update-server.ps1", "Рем. зона", "Простой BDC", "kpi_day_flags",
                   "plctime.py", "Остаток кузовов на ФЛ", "status.json", "APP_ENV", "ORCHESTRATOR.md", "Andon-Reports"]:
        assert phrase in text, phrase
    assert "аналитика аварий" in REF.lower()
