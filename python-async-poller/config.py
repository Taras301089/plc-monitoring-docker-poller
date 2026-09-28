from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or raw == "" else int(raw)
    if value <= 0:
        raise RuntimeError(f"Переменная окружения {name} должна быть больше нуля")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_min_pool: int
    postgres_max_pool: int
    config_reload_sec: int
    log_level: str

    def __post_init__(self) -> None:
        if self.postgres_min_pool > self.postgres_max_pool:
            raise RuntimeError("POSTGRES_MIN_POOL не может быть больше POSTGRES_MAX_POOL")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{quote_plus(self.postgres_user)}:{quote_plus(self.postgres_password)}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def load_settings() -> Settings:
    return Settings(
        postgres_host=_require("POSTGRES_HOST"),
        postgres_port=_int("POSTGRES_PORT", 5432),
        postgres_db=_require("POSTGRES_DB"),
        postgres_user=_require("POSTGRES_USER"),
        postgres_password=_require("POSTGRES_PASSWORD"),
        postgres_min_pool=_int("POSTGRES_MIN_POOL", 2),
        postgres_max_pool=_int("POSTGRES_MAX_POOL", 10),
        config_reload_sec=_int("CONFIG_RELOAD_SEC", 30),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
