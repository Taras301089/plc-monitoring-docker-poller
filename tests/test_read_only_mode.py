"""Режим «только чтение» тестовой копии: логика блокировки и защитные настройки docker-compose.dev.yml (база не нужна)."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "fastapi-api"))

from readonly import blocked  # noqa: E402

DEV = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_read_methods_allowed(method):
    assert blocked(method, True) is False


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_blocked_in_read_only(method):
    assert blocked(method, True) is True


def test_reading_plc_values_by_post_allowed():
    # живые значения с ПЛК запрашиваются POST, но это только чтение
    assert blocked("POST", True, "/api/plcs/7/opcua/values") is False


@pytest.mark.parametrize("path", ["/api/kpi/screens/1/plan-day", "/api/plcs/7/opcua/values/x", "/api/plcs/7"])
def test_other_posts_blocked(path):
    assert blocked("POST", True, path) is True


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_everything_allowed_without_read_only(method):
    assert blocked(method, False) is False


def test_dev_compose_isolated():
    assert re.search(r"^name:\s*pmm-test\s*$", DEV, re.M)
    assert '"127.0.0.1:8001:8000"' in DEV
    assert 'READ_ONLY: "1"' in DEV
    assert re.search(r"APP_ENV:\s*dev\b", DEV)
    assert "pmm_readonly" in DEV
    for name in ("fastapi_api", "postgres_db", "python-async-poller"):
        assert not re.search(rf"container_name:\s*{re.escape(name)}\s*$", DEV, re.M)
