"""Кэш сессии между запусками.

Стенд жёстко лимитирует частоту запросов кода входа. Поэтому токен, полученный
один раз, сохраняется на диск и переиспользуется всеми последующими запусками —
и скриптом, и тестами. Это снимает лимит как класс: код запрашивается только
когда сохранённая сессия протухла.

Файл лежит рядом с проектом и попадает в .gitignore: в нём живой токен.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("lo_api.session")


@dataclass
class CachedSession:
    phone: str
    token: str
    passport_id: str
    legal_entity_id: str = ""
    device_id: str = ""
    saved_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.saved_at

    def __str__(self) -> str:
        return f"сессия {self.phone}, возраст {self.age_seconds / 60:.0f} мин"


def _path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def load(raw_path: str, *, phone: str | None = None,
         max_age: float | None = None) -> CachedSession | None:
    """Читает сохранённую сессию. None — если её нет, она чужая или протухла."""
    path = _path(raw_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached = CachedSession(**data)
    except (ValueError, TypeError) as exc:
        log.warning("Кэш сессии повреждён (%s), игнорирую: %s", path, exc)
        return None

    if not cached.token:
        return None
    if phone and cached.phone != phone:
        log.info("В кэше сессия другого аккаунта (%s), нужен %s", cached.phone, phone)
        return None
    if max_age is not None and cached.age_seconds > max_age:
        log.info("Сессия в кэше старше %.0f мин — обновляю", max_age / 60)
        return None
    return cached


def save(raw_path: str, session: CachedSession) -> None:
    path = _path(raw_path)
    session.saved_at = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)  # в файле живой токен
    log.debug("Сессия сохранена в %s", path)


def clear(raw_path: str) -> None:
    path = _path(raw_path)
    if path.exists():
        path.unlink()
        log.info("Кэш сессии очищен: %s", path)
