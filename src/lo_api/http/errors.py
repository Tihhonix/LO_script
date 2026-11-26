"""Исключения транспортного слоя.

Библиотека НЕ содержит assert'ов: она поднимает исключения. Ассерты — дело
тестов. Благодаря этому те же клиенты используются интерактивным раннером
(lo_api.cli.run_flow), где pytest отсутствует.
"""

from __future__ import annotations

from typing import Any

# Признаки того, что имя хоста не разрешилось в адрес. requests оборачивает
# ошибку резолвера в несколько слоёв, поэтому ищем по тексту.
_DNS_MARKERS = (
    "nameresolutionerror",
    "failed to resolve",
    "nodename nor servname",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo failed",
)


def network_error_text(exc: Exception, host: str = "") -> str:
    """Текст сетевой ошибки с подсказкой, что делать.

    Все стенды Ленты живут во внутренней сети. Если имя хоста не разрешается,
    дело почти всегда в отключённом VPN, а не в стенде — но по исходному
    тексту исключения этого не видно, и ошибка выглядит как поломка скрипта.
    """
    text = str(exc)
    if any(marker in text.lower() for marker in _DNS_MARKERS):
        # В host прилетает и голое имя, и полный URL запроса — в сообщении
        # нужно только имя, иначе подсказка тонет в пути эндпоинта
        name = host.split("//")[-1].split("/")[0]
        where = f" {name}" if name else ""
        return (f"сетевая ошибка: имя хоста{where} не разрешается в адрес. Похоже, "
                f"не подключён VPN: стенды видны только из внутренней сети. "
                f"Подключитесь и повторите")
    return f"сетевая ошибка: {text}"


class LoApiError(Exception):
    """Базовая ошибка обращения к API."""

    def __init__(self, method: str, message: str, *, status: int | None = None,
                 payload: Any = None, trace_id: str | None = None) -> None:
        self.method = method
        self.status = status
        self.payload = payload
        self.trace_id = trace_id
        parts = [f"{method}: {message}"]
        if status is not None:
            parts.append(f"HTTP {status}")
        if trace_id:
            parts.append(f"x-trace-id={trace_id}")
        super().__init__(" | ".join(parts))


class HttpStatusError(LoApiError):
    """Ответ пришёл с неожиданным HTTP-статусом."""


class NotJsonError(LoApiError):
    """Тело ответа не разбирается как JSON."""


class BusinessError(LoApiError):
    """HTTP 200, но в теле ответа — ошибка.

    Легаси-эндпоинты Ленты (/api/rest/*) и JSON-RPC отвечают 200 на бизнес-
    ошибку. Это тот класс ошибок, который старый скрипт не замечал вообще.
    """

    def __init__(self, method: str, error: Any, *, payload: Any = None,
                 trace_id: str | None = None) -> None:
        self.error = error
        super().__init__(method, f"бизнес-ошибка {error!r}", status=200,
                         payload=payload, trace_id=trace_id)
