"""Справка: раздел «Схема проекта» (схемы Mermaid). Код схем корректен, библиотека и лицензия лежат в репозитории (база не нужна)."""
import html
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fastapi-api" / "static"
REF = (STATIC / "reference.html").read_text(encoding="utf-8")
CODE = re.compile(r'<div class="code-block" id="(scheme-code-\d)" data-copy>(.*?)</div>', re.S)
DEF = re.compile(r'^\s+([A-Z][A-Z0-9_]*)[\[\(]', re.M)
EDGE = re.compile(r'^\s+([A-Z][A-Z0-9_]*)\s*(?:<-->|-->|-\.->|~~~)(?:\|[^|]*\|)?\s*([A-Z][A-Z0-9_]*)\s*$', re.M)


def schemes():
    return [(i, html.unescape(c)) for i, c in CODE.findall(REF)]


def test_three_schemes_in_reference():
    s = schemes()
    assert [i for i, _ in s] == ["scheme-code-1", "scheme-code-2", "scheme-code-3"]
    assert REF.count('class="scheme-box"') == 3
    for i, _ in s:
        assert f'data-scheme="{i}"' in REF, f"блок рисования для {i} не найден"


def test_scheme_code_is_valid_enough():
    for i, code in schemes():
        assert re.match(r"flowchart (LR|TB)\n", code), i
        for cls in ("ready", "wip", "plan"):
            assert f"classDef {cls} " in code, f"{i}: нет класса {cls}"
        defined = set(DEF.findall(code))
        assert len(defined) >= 5, i
        for a, b in EDGE.findall(code):
            assert a in defined and b in defined, f"{i}: связь {a} -> {b} ссылается на блок без описания"
        for line in code.splitlines():
            if DEF.match(line):
                assert re.search(r":::(ready|wip|plan)\s*$", line), f"{i}: у блока нет статуса: {line.strip()[:50]}"


def test_legend_and_status_date_present():
    assert "Состояние на " in REF
    for cls in ("sch-ok", "sch-wip", "sch-plan"):
        assert cls in REF


def test_mermaid_library_and_license_are_local():
    lib = STATIC / "vendor" / "mermaid.min.js"
    assert lib.is_file() and lib.stat().st_size > 1_000_000, "библиотека Mermaid должна лежать в static/vendor"
    lic = (STATIC / "vendor" / "mermaid.LICENSE.txt").read_text(encoding="utf-8")
    assert "MIT License" in lic and "Knut Sveidqvist" in lic
    assert '<script src="/static/vendor/mermaid.min.js" defer></script>' in REF
    assert '<script src="/static/scheme.js" defer></script>' in REF
    assert (STATIC / "scheme.js").is_file()
    assert "cdn." not in REF.split("scheme-section")[1].split("</section>")[0], "схема не должна грузиться из интернета"
