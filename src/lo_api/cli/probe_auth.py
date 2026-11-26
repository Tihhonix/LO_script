"""Диагностика путей авторизации на стенде.

Отвечает на вопрос «по какому пути живёт verify», не тратя попытки ввода кода:
проверяется только маршрутизация. 404 «Page not found» означает, что пути нет;
любой другой ответ (400 / 401 / 422 / 500) означает, что путь есть, а запрос
просто отвергнут по содержимому — это и есть искомый признак.

Код входа НЕ запрашивается, реальный номер не используется.

    python -m lo_api.cli.probe_auth
"""

from __future__ import annotations

import json
import sys

import requests

from lo_api.config import load_settings

# Заведомо невалидные данные: нам нужен только код маршрутизации
PROBE_BODY = {"phone": "0000000000", "code": "0000", "kfpKsid": "probe"}

CANDIDATES = [
    "/pro/api-gateway/v1/auth/code/verify",
    "/api-gateway/v1/auth/code/verify",
    "/pro/api-gateway/v1/auth/code/verify/",
    "/api-gateway/v1/pro/auth/code/verify",
]


def probe(base_url: str, path: str, timeout: float = 15.0) -> tuple[int | None, str]:
    url = base_url.rstrip("/") + path
    try:
        r = requests.post(
            url, data=json.dumps(PROBE_BODY),
            headers={"Content-Type": "application/json", "accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"сетевая ошибка: {exc}"
    return r.status_code, r.text[:180].replace("\n", " ")


def main() -> int:
    cfg = load_settings()
    print(f"Стенд: {cfg.base_url}\n")

    send_status, send_body = probe(cfg.base_url, cfg.auth_send_path)
    print(f"send   {cfg.auth_send_path}")
    print(f"       → {send_status}  {send_body}\n")

    print("verify — проверка маршрутизации (код входа не запрашивается):")
    found: list[str] = []
    for path in CANDIDATES:
        status, body = probe(cfg.base_url, path)
        verdict = "путь ЕСТЬ" if status not in (None, 404) else "нет"
        if status not in (None, 404):
            found.append(path)
        print(f"  {status if status is not None else '---':>4}  {verdict:<10} {path}")
        print(f"        {body}")

    print()
    if not found:
        print("Ни один кандидат не отвечает. Возможные причины:")
        print("  • стенд лежит или сменил адрес (проверьте LO_BASE_URL);")
        print("  • verify живёт на другом хосте — снимите реальный путь из DevTools")
        print("    браузера на этом стенде (вкладка Network, фильтр 'verify').")
        return 1

    best = found[0]
    print(f"Рабочий путь: {best}")
    if best != cfg.auth_verify_path:
        print(f"Пропишите в .env:\n  LO_AUTH_VERIFY_PATH={best}")
    else:
        print("Он уже стоит в конфиге — правки не нужны.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
