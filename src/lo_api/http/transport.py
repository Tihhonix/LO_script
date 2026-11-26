"""Единый HTTP-транспорт вместо 22 блоков скопированных заголовков.

Здесь живёт всё, что раньше дублировалось: базовые заголовки, конверт
Head/Body, таймауты, ретраи, трассировка и разбор ошибок.

Три вида эндпоинтов у API Ленты, три метода:
  * rest()     — /api/rest/<Method>, тело как request=<urlencoded JSON>
  * jrpc()     — /jrpc/<method>, JSON-RPC 2.0
  * gateway()  — /api-gateway/v1/..., обычный JSON

Cookie-заголовков здесь нет намеренно: подтверждено, что Head.SessionToken
достаточно всегда. Старые Utk_SssTkn / SOURCE_ID_time были мусором (и в пяти
местах — багом: токен подставлялся в куку временной метки).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from lo_api.config import Settings
from lo_api.constants import CLIENT_VERSION, Mission
from lo_api.http.errors import (
    BusinessError,
    HttpStatusError,
    NotJsonError,
    network_error_text,
)
from lo_api.http.trace import new_trace, request_id

log = logging.getLogger("lo_api.http")

RETRYABLE_STATUSES = frozenset({502, 503, 504})


def pretty_body(body: Any, limit: int | None) -> str:
    """Тело запроса или ответа с отступами.

    Легаси-конверт приезжает как {"request": "<json-строка>"} — вложенный JSON
    разворачивается, иначе в логе видно экранированную кашу вместо Head/Body.
    """
    if isinstance(body, dict) and body.keys() == {"request"}:
        body = body["request"]
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        value = json.loads(body) if isinstance(body, str) else body
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        text = str(body)
    if limit is not None and len(text) > limit:
        return f"{text[:limit]}\n… обрезано до {limit} символов (LO_LOG_BODY_LIMIT)"
    return text


def _error_list_text(errors: list) -> str:
    parts = [
        str(e.get("Message") or e.get("Description") or e) if isinstance(e, dict) else str(e)
        for e in errors
    ]
    return "; ".join(parts)


@dataclass
class Context:
    """Состояние сессии, которое накапливается по ходу флоу.

    Раньше это были разрозненные глобальные переменные с `global`-мутациями.
    """

    cfg: Settings
    session_token: str | None = None
    passport_id: str | None = None
    legal_entity_id: str | None = None
    mission: Mission = Mission.PICKUP
    selected_address_id: int | None = None
    store_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def mpk(self) -> str:
        """MPK всегда соответствует текущей миссии — рассинхрон невозможен."""
        return self.cfg.mpk(self.mission)

    @property
    def authorized(self) -> bool:
        return bool(self.session_token)


class Transport:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.cfg
        self._session = requests.Session()
        self._session.headers.update(self._static_headers())

    # -- заголовки ---------------------------------------------------------
    def _static_headers(self) -> dict[str, str]:
        cfg = self.cfg
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
            "origin": cfg.base_url,
            "referer": cfg.base_url + "/",
            "user-agent": cfg.user_agent,
            "client": CLIENT_VERSION,
            "app-version": "0.0.2",
            "x-device-web-platform": "desktop_web",
            "x-domain": cfg.domain,
            "x-retail-brand": cfg.retail_brand,
            "x-platform": cfg.platform,
            "x-device-os": cfg.device_os,
            "x-device-os-version": cfg.device_os_version,
        }

    def _dynamic_headers(self) -> dict[str, str]:
        """Заголовки, зависящие от текущего состояния сессии.

        Ключевое отличие от старого кода: passport-id и organization-id берутся
        из контекста, а не из захардкоженных литералов. Раньше выбор организации
        был фикцией — заказ всегда уходил под одной и той же.
        """
        ctx = self.ctx
        h: dict[str, str] = {
            "deviceid": self.device_id,
            "x-device-id": self.device_id,
            "x-delivery-mode": str(ctx.mission),
            "marketingpartnerkey": ctx.mpk,
        }
        if ctx.session_token:
            h["sessiontoken"] = ctx.session_token
        if ctx.passport_id:
            h["x-passport-id"] = ctx.passport_id
        if ctx.legal_entity_id:
            h["x-organization-id"] = ctx.legal_entity_id
        return h

    @property
    def device_id(self) -> str:
        """Один DeviceId на весь прогон.

        В старом коде их было два разных ("68260536-..." и "d5a0f26a-...") —
        случайная копипаста из двух разных сессий браузера.
        """
        did = self.ctx.extra.get("device_id")
        if not did:
            from uuid import uuid4

            did = str(uuid4())
            self.ctx.extra["device_id"] = did
        return did

    # -- низкий уровень ----------------------------------------------------
    def request(
        self,
        http_method: str,
        url: str,
        *,
        label: str,
        retry: bool = True,
        expected_status: int | tuple[int, ...] = 200,
        **kwargs: Any,
    ) -> requests.Response:
        """Один HTTP-вызов с таймаутом, ретраем и логированием.

        retry=False обязателен для неидемпотентных вызовов (orderAdd):
        повтор на 502 может создать второй заказ.
        """
        headers = {**self._dynamic_headers(), **kwargs.pop("headers", {}), **new_trace()}
        kwargs.setdefault("timeout", self.cfg.timeout)
        attempts = self.cfg.retry_attempts if retry else 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            trace_id = headers["x-trace-id"]
            started = time.monotonic()
            try:
                resp = self._session.request(http_method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: попытка %s/%s — %s", label, attempt, attempts, exc)
                if attempt == attempts:
                    raise HttpStatusError(label, network_error_text(exc, url),
                                          trace_id=trace_id) from exc
                time.sleep(self.cfg.retry_backoff * attempt)
                headers.update(new_trace())
                continue

            elapsed = (time.monotonic() - started) * 1000
            self._log(label, http_method, url, kwargs, resp, elapsed, trace_id)

            if resp.status_code in RETRYABLE_STATUSES and attempt < attempts:
                log.warning("%s: HTTP %s, повтор %s/%s",
                            label, resp.status_code, attempt, attempts)
                time.sleep(self.cfg.retry_backoff * attempt)
                headers.update(new_trace())
                continue

            expected = (expected_status,) if isinstance(expected_status, int) else expected_status
            if resp.status_code not in expected:
                raise HttpStatusError(
                    label,
                    f"ожидался статус {expected}, тело: {resp.text[:500]}",
                    status=resp.status_code, payload=resp.text, trace_id=trace_id,
                )
            return resp

        raise HttpStatusError(label, f"исчерпаны попытки: {last_exc}")

    def _log(self, label, http_method, url, kwargs, resp, elapsed_ms, trace_id) -> None:
        log.info("%-34s %s %s → %s (%.0f ms) trace=%s",
                 label, http_method, url, resp.status_code, elapsed_ms, trace_id)
        if not self.cfg.log_bodies:
            return
        # В файл тела пишутся целиком: обрезанный JSON нечитаем и негрепаем,
        # а ради него файл и заводят. Лимит остаётся для вывода в консоль.
        limit = None if (self.cfg.log_file or self.cfg.log_dir) else self.cfg.log_body_limit
        body = kwargs.get("data") or kwargs.get("json")
        if body:
            log.debug("запрос %s:\n%s", label, pretty_body(body, limit))
        log.debug("ответ %s:\n%s", label, pretty_body(resp.text, limit))

    def _parse(self, resp: requests.Response, label: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise NotJsonError(label, f"ответ не JSON: {resp.text[:500]}",
                               status=resp.status_code, payload=resp.text) from exc

    # -- транспорт 1: легаси REST -----------------------------------------
    def rest(self, method: str, body: dict | None = None, *,
             retry: bool = True, raise_on_business_error: bool = True) -> dict:
        """POST /api/rest/<method> с конвертом Head/Body.

        Конверт собирается ровно в одном месте — раньше он был скопирован
        девять раз, и в четырёх копиях разошёлся.
        """
        envelope = {
            "Head": {
                "MarketingPartnerKey": self.ctx.mpk,
                "Version": CLIENT_VERSION,
                "Client": CLIENT_VERSION,
                "Method": method,
                "RequestId": request_id(method),
                "DeviceId": self.device_id,
                "Domain": self.cfg.domain,
                "SessionToken": self.ctx.session_token,
            },
            "Body": body if body is not None else {},
        }
        resp = self.request(
            "POST", urljoin(self.cfg.base_url, f"/api/rest/{method}"),
            label=method, retry=retry,
            data={"request": json.dumps(envelope, ensure_ascii=False)},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = self._parse(resp, method)
        if raise_on_business_error:
            self._check_rest_error(method, payload, resp.headers.get("x-trace-id"))
        return payload

    @staticmethod
    def _check_rest_error(method: str, payload: Any, trace_id: str | None) -> None:
        """Единая проверка бизнес-статуса легаси-ответа.

        Легаси-эндпоинты отвечают 200 даже на ошибку. Старый скрипт этого не
        ловил вообще — тест был зелёным при отказе бэкенда.
        """
        if not isinstance(payload, dict):
            return
        head = payload.get("Head") or {}

        # Сначала ищем КОНКРЕТНУЮ причину и только потом падаем по статусу:
        # Head.Status='failure' сам по себе не говорит ничего, и раньше он
        # бросался первым, пряча настоящее объяснение из того же ответа.
        for holder in (head, payload, payload.get("Body") or {}):
            if not isinstance(holder, dict):
                continue
            err = holder.get("Error") or holder.get("ErrorMessage")
            code = holder.get("ErrorCode")
            if err or (code not in (None, 0, "0", "")):
                raise BusinessError(method, err or code, payload=payload, trace_id=trace_id)

            # ErrorList — так стенд объясняет отказ, оставаясь на 200 с пустым
            # результатом («Все курьеры пока заняты» вместо списка интервалов).
            # Без этой проверки причина терялась, и вызывающий код сообщал лишь
            # «список пуст», отправляя разбираться не туда.
            errors = holder.get("ErrorList")
            if isinstance(errors, list) and errors:
                raise BusinessError(method, _error_list_text(errors),
                                    payload=payload, trace_id=trace_id)

        # Легаси-конверт несёт бизнес-статус в Head.Status.
        # "success" — успех, "pending" — принято в обработку (так отвечает
        # orderAdd), всё остальное считаем ошибкой.
        status = head.get("Status")
        if isinstance(status, str) and status.lower() not in ("success", "pending", ""):
            raise BusinessError(
                method,
                f"Head.Status={status!r}, причины в ответе нет. Тело: {payload}",
                payload=payload, trace_id=trace_id,
            )

    # -- транспорт 2: JSON-RPC --------------------------------------------
    def jrpc(self, method: str, params: dict | None = None, *,
             retry: bool = True, raise_on_business_error: bool = True) -> dict:
        body = {
            "method": method,
            "params": params or {},
            "jsonrpc": "2.0",
            "id": request_id(method),
        }
        resp = self.request(
            "POST", urljoin(self.cfg.base_url, f"/jrpc/{method}"),
            label=method, retry=retry,
            data=json.dumps(body, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
        payload = self._parse(resp, method)
        if raise_on_business_error and isinstance(payload, dict) and payload.get("error"):
            raise BusinessError(method, payload["error"], payload=payload,
                                trace_id=resp.headers.get("x-trace-id"))
        return payload

    # -- транспорт 3: api-gateway -----------------------------------------
    def gateway(self, path: str, *, http_method: str = "POST", label: str | None = None,
                json_body: dict | None = None, retry: bool = True,
                expected_status: int | tuple[int, ...] = 200,
                base_url: str | None = None,
                headers: dict[str, str] | None = None) -> Any:
        label = label or path.rsplit("/", 1)[-1]
        resp = self.request(
            http_method, urljoin(base_url or self.cfg.base_url, path),
            label=label, retry=retry, expected_status=expected_status,
            data=json.dumps(json_body, ensure_ascii=False) if json_body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        return self._parse(resp, label)

    def close(self) -> None:
        self._session.close()
