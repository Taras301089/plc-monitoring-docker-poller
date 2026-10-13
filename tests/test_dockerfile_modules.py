"""Каждый модуль fastapi-api/*.py должен быть в списке COPY у Dockerfile, иначе контейнер API не запустится (ModuleNotFoundError)."""
import re
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "fastapi-api"


def test_all_modules_are_copied_into_the_image():
    docker = (API / "Dockerfile").read_text(encoding="utf-8")
    copied = set()
    for line in docker.splitlines():
        if line.strip().startswith("COPY ") and ".py" in line:
            copied |= {t for t in line.split()[1:-1] if t.endswith(".py")}
    modules = {p.name for p in API.glob("*.py")}
    assert modules <= copied, f"не копируются в образ: {sorted(modules - copied)}"
