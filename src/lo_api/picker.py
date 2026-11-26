"""Общий транспорт picker-service-proxy: JSON-RPC по `/jrpc`.

На нём висят и вывод на смену (`workshift.py`), и сборка с доставкой
(`picking.py`): один хост, одни заголовки, один способ входа. К API Ленты
(`http/transport.py` с конвертом Head/Body) отношения не имеет.

Вход: `authTokenGet` по логину и паролю сотрудника, дальше токен едет
в заголовке `SessionToken`.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

import requests

from lo_api.config import Settings
from lo_api.http.errors import LoApiError, network_error_text
from lo_api.http.transport import pretty_body

log = logging.getLogger("lo_api.picker")

# Признаки временного сбоя стенда, а не отказа бизнес-логики: связка сервисов
# отваливается, и тот же запрос проходит со второй-третьей попытки.
TRANSIENT_MARKERS = (
    "error proxy",
    "rpc error",
    "не удалось получить информацию",
    "timeout",
)

# Признаки того, что сервис или его база недоступны целиком. Повторять и
# наращивать число попыток бесполезно — чинится только на стороне стенда.
UNREACHABLE_MARKERS = (
    "connection refused",
    "dial tcp",
    "connection error",
    "no route to host",
    # DNS: имя внутреннего сервиса не резолвится — его просто нет в кластере
    "no such host",
    "hostname resolving error",
    "name resolution",
    "server misbehaving",
)


class PickerError(LoApiError):
    """Ошибка обращения к picker-service.

    Текст исключения — для человека: ответ стенда плюс совет скрипта. Тем, кто
    объясняет ошибку по-своему, нужен ответ стенда без чужого совета — он в
    `reason`, а `unreachable` говорит, что сервис не ответил вовсе.
    """

    reason: str = ""
    unreachable: bool = False


class PickerOrderNotFound(PickerError):
    """orderGet не нашёл заказ на выбранном стенде."""


def tries(count: int) -> str:
    """«за 1 попытку», «за 3 попытки», «за 5 попыток»."""
    last2, last = count % 100, count % 10
    if last == 1 and last2 != 11:
        word = "попытку"
    elif 2 <= last <= 4 and not 12 <= last2 <= 14:
        word = "попытки"
    else:
        word = "попыток"
    return f"за {count} {word}"


def _error(method: str, reason: str, message: str, *, unreachable: bool = False,
           status: int | None = None, payload: Any = None) -> PickerError:
    exc = PickerError(method, message, status=status, payload=payload)
    exc.reason = reason
    exc.unreachable = unreachable
    return exc


def error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("description") or error)
    return str(error or "")


def is_transient(status: int, error: Any) -> bool:
    """Стоит ли повторить запрос."""
    if status >= 500:
        return True
    return any(m in error_text(error).lower() for m in TRANSIENT_MARKERS)


class PickerRpc:
    """Сессия picker-service. Токен хранится в клиенте, а не в конфиге."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.url = f"https://{cfg.picker_proxy_url.strip('/')}/jrpc"
        self.token: str | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json",
            "deviceId": cfg.picker_device_id,
            "Client": cfg.picker_client,
            "MarketingPartnerId": str(cfg.mp_picker_id),
            "MarketingPartnerKey": cfg.mp_picker_key,
        })

    # -- транспорт ---------------------------------------------------------
    def call(self, method: str, params: dict | None = None, *,
             token: str | None = None, attempts: int | None = None) -> dict:
        """Вызов с повтором временных сбоев стенда.

        Каждая попытка получает свой id: стенд отвечает на повтор как на новый
        запрос, а не отдаёт закэшированную ошибку. `attempts=1` отключает
        повторы для методов, падение которых на стенде — норма.
        """
        headers = {}
        session_token = token or self.token
        if session_token:
            headers["SessionToken"] = session_token
        attempts = max(1, attempts if attempts is not None
                       else self.cfg.workshift_retry_attempts)
        reason = ""

        for attempt in range(1, attempts + 1):
            body = {"jsonrpc": "2.0", "method": method, "id": str(uuid4()),
                    "params": params if params is not None else {}}
            status, payload, reason = self._attempt(method, body, headers)
            if status == 200 and payload is not None and not payload.get("error"):
                return payload.get("result") or {}

            error = (payload or {}).get("error")
            if method == "orderGet" and error and "order not found" in reason.lower():
                raise PickerOrderNotFound(method, f"{reason}. Проверьте номер заказа и стенд "
                                          f"({self.cfg.picker_proxy_url}).",
                                          status=status, payload=payload)
            batch_lookup_busy = (method == "getBatchMarkingInfo"
                                 and "повторите попытку позже" in reason.casefold())
            if not (batch_lookup_busy or is_transient(status, error)):
                raise _error(method, reason, reason, status=status or None, payload=payload)
            if attempt < attempts:
                log.warning("%s: %s — повтор %s/%s", method, reason, attempt, attempts)
                time.sleep(self.cfg.retry_backoff * attempt)

        # Недоступность сервиса или его базы (в том числе нерезолвящийся DNS)
        # повторами не лечится — чинить надо стенд
        unreachable = any(m in reason.lower() for m in UNREACHABLE_MARKERS)
        advice = ("Похоже, сервис или его база недоступны — чинится на стороне стенда. "
                  "Увеличивать число попыток бесполезно"
                  if unreachable else
                  "Повторите позже или увеличьте LO_WORKSHIFT_RETRY_ATTEMPTS")
        raise _error(method, reason, f"{reason}. Не удалось {tries(attempts)}. {advice}",
                     unreachable=unreachable, payload=payload)

    def _attempt(self, method: str, body: dict,
                 headers: dict) -> tuple[int, dict | None, str]:
        """Одна попытка. Возвращает статус, разобранное тело и причину отказа."""
        try:
            resp = self.session.post(self.url, json=body, headers=headers,
                                     timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            return 0, None, network_error_text(exc, self.cfg.picker_proxy_url)

        log.info("%-30s POST %s → %s", method, self.url, resp.status_code)
        if self.cfg.log_bodies:
            limit = None if (self.cfg.log_file or self.cfg.log_dir) else self.cfg.log_body_limit
            log.debug("запрос %s:\n%s", method, pretty_body(body, limit))
            log.debug("ответ %s:\n%s", method, pretty_body(resp.text, limit))
        try:
            payload = resp.json()
        except ValueError:
            return resp.status_code, None, f"ответ не JSON: {resp.text[:200]}"

        error = payload.get("error")
        if resp.status_code == 200 and not error:
            return resp.status_code, payload, ""
        return resp.status_code, payload, error_text(error) or f"HTTP {resp.status_code}"

    # -- вход --------------------------------------------------------------
    @staticmethod
    def require_credentials(login: str, password: str, prefix: str) -> None:
        """Пустые логин и пароль ловятся до сети.

        На пустой username стенд отвечает «Отсутствует обязательный параметр»,
        и по этому тексту не догадаться, что дело в незаполненном .env.
        """
        if login and password:
            return
        raise PickerError(
            "authTokenGet",
            f"не заданы {prefix}_LOGIN и {prefix}_PASSWORD в .env — без них вывести "
            f"на смену нельзя. Логин должен совпадать с Id сотрудника в списках TMS",
        )

    def login(self, username: str, password: str) -> str:
        """Токен сотрудника. Он же едет в SessionToken последующих вызовов."""
        result = self.call("authTokenGet", {"username": username, "password": password},
                           token="")
        token = result.get("token")
        if not token:
            raise PickerError(
                "authTokenGet",
                f"в ответе нет токена для {username!r}. Проверьте логин и пароль "
                f"в .env (LO_PICKER_LOGIN / LO_COURIER_LOGIN и пароли к ним)",
            )
        self.token = token
        return token

    def close(self) -> None:
        self.session.close()
