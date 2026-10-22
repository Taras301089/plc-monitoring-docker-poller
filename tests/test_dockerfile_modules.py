"""Root build context includes every API module and SCADA, without local data."""
import fnmatch
import re
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "fastapi-api"


def _ignore_patterns():
    f = API / "Dockerfile.dockerignore"
    if not f.exists():
        return []
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")]


def test_dockerfile_copies_all_py_with_one_glob():
    docker = (API / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY\s+fastapi-api/\*\.py\s+\./?\s*$", docker, re.M)
    assert not re.search(r"^COPY\s+(?:fastapi-api/)?main\.py", docker, re.M), "поимённый список модулей возвращать нельзя"
    assert re.search(r"^COPY\s+Scada\s+\./Scada\s*$", docker, re.M)


def test_no_module_is_excluded_by_dockerignore():
    patterns = _ignore_patterns()
    assert patterns[0] == "**", "root build context must exclude everything by default"
    assert "!fastapi-api/*.py" in patterns
    assert "!Scada/gwm/*.py" in patterns and "!Scada/gwm/static/**" in patterns
    modules = sorted(p.name for p in API.glob("*.py"))
    assert "main.py" in modules
    for name in modules:
        # The source glob must not be overridden by a later exclusion.
        for pat in patterns[patterns.index("!fastapi-api/*.py") + 1:]:
            if not pat.startswith("!"):
                assert not fnmatch.fnmatch(f"fastapi-api/{name}", pat.lstrip("/")), f"{name} excluded by {pat}"
    allowed = [p for p in patterns if p.startswith("!")]
    assert all(p.startswith(("!fastapi-api/", "!Scada/")) for p in allowed)
