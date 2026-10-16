"""Режим «только чтение» для тестовой копии сайта (READ_ONLY=1): база подключена пользователем pmm_readonly,
запись запрещена и самой базой, и этим слоем (чтобы копия не могла через поллер записать в ПЛК)."""
from __future__ import annotations

import os
import re
from typing import Any

READ_ONLY = os.getenv("READ_ONLY", "").strip().lower() in ("1", "true", "yes")

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# Синтетический пользователь той же формы, что возвращает auth.current_user: вход в тестовой копии не нужен
READONLY_USER: dict[str, Any] = {
    "id": 0,
    "login": "readonly",
    "last_name": "Только чтение",
    "first_name": "",
    "full_name": "Только чтение",
    "role": "admin",
    "role_name": "Администратор",
    "department": None,
    "department_name": "",
    "must_change_password": False,
}


# POST-запросы, которые только читают: живые значения переменных с ПЛК (поллер вызывает read_values, без записи)
_READ_POSTS = re.compile(r"^/api/plcs/\d+/opcua/values$")


def blocked(method: str, read_only: bool, path: str = "") -> bool:
    """True, если запрос нужно отклонить (в режиме чтения разрешены GET/HEAD/OPTIONS и POST-чтение значений с ПЛК)."""
    if not read_only or method.upper() in _SAFE_METHODS:
        return False
    return not (method.upper() == "POST" and _READ_POSTS.match(path))
