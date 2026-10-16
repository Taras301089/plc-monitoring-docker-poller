"""Журнал поллера без шума: записи по каждому запросу сайта (aiohttp.access) и чтению asyncua скрыты, ошибки остаются.

LOG_LEVEL=DEBUG возвращает полный журнал для разбора проблем."""
import re
from pathlib import Path

MAIN = (Path(__file__).resolve().parent.parent / "python-async-poller" / "main.py").read_text(encoding="utf-8")


def test_noisy_loggers_are_quieted_except_in_debug():
    block = MAIN[MAIN.index("logging.basicConfig("):MAIN.index("db = Database(settings)")]
    assert re.search(r'if settings\.log_level != "DEBUG":', block)
    assert '"aiohttp.access"' in block and '"asyncua"' in block
    assert "setLevel(logging.WARNING)" in block
    # приложение при этом пишет на уровне из настроек (INFO по умолчанию), а не WARNING
    assert "level=getattr(logging, settings.log_level, logging.INFO)" in block
