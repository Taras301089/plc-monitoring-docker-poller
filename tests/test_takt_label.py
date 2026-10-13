"""Подпись «Takt Time»: везде одна (английские слова, T и T большие), без «Такт-тайм» и «за период»: Andon, архив, справка, Excel."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "fastapi-api" / "static"
FILES = [*STATIC.glob("*.html"), *STATIC.glob("*.js"), *STATIC.glob("*.css"),
         *(ROOT / "fastapi-api").glob("*.py"), *(ROOT / "python-async-poller").glob("*.py")]
sys.path.insert(0, str(ROOT / "fastapi-api"))
OLD = re.compile(r"такт[- ]тайм", re.I)


def test_no_old_takt_label_in_sources():
    bad = [f.name for f in FILES if OLD.search(f.read_text(encoding="utf-8"))]
    assert bad == [], bad


def test_no_long_takt_label_anywhere():
    bad = [f.name for f in FILES if re.search(r"takt\s*time\s+за\s+период", f.read_text(encoding="utf-8"), re.I)]
    assert bad == [], bad


def test_excel_day_header_uses_new_label():
    from history import DAY_HEADERS
    assert "Takt Time" in DAY_HEADERS
    assert not any(OLD.search(h) for h in DAY_HEADERS)


def test_andon_table_header_and_tile():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count(">Takt Time</th>") >= 2
    assert html.count("<label>Takt Time</label>") >= 2
