"""Новый модуль fastapi-api/*.py должен попасть в образ без правки Dockerfile: общий COPY *.py и ни один модуль не исключён .dockerignore."""
import fnmatch
import re
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "fastapi-api"


def _ignore_patterns():
    f = API / ".dockerignore"
    if not f.exists():
        return []
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")]


def test_dockerfile_copies_all_py_with_one_glob():
    docker = (API / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY\s+\*\.py\s+\./?\s*$", docker, re.M), "в Dockerfile должна быть строка COPY *.py ./"
    assert not re.search(r"^COPY\s+main\.py", docker, re.M), "поимённый список модулей возвращать нельзя"


def test_no_module_is_excluded_by_dockerignore():
    patterns = _ignore_patterns()
    modules = sorted(p.name for p in API.glob("*.py"))
    assert "main.py" in modules
    for name in modules:
        for pat in patterns:
            pat = pat.lstrip("/")
            assert not fnmatch.fnmatch(name, pat) and pat != name, f"{name} исключён .dockerignore ({pat}), в образ не попадёт"
    assert "Dockerfile" not in [p for p in patterns if p.startswith("!")], "исключения-отрицания не используем"
