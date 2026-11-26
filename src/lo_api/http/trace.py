"""Генерация трассировочных заголовков.

В старом скрипте traceparent / x-trace-id / x-span-id были захардкожены
одинаковыми на все прогоны — все запуски сливались в один трейс, и найти
конкретный запрос в Jaeger/Sentry было невозможно.

Здесь каждый запрос получает свой trace_id, который логируется и попадает в
текст исключения — по нему упавший тест ищется в трассировке за секунды.
"""

from __future__ import annotations

from uuid import uuid4


def new_trace() -> dict[str, str]:
    trace_id = uuid4().hex           # 32 hex
    span_id = uuid4().hex[:16]       # 16 hex
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "x-trace-id": trace_id,
        "x-span-id": span_id,
    }


def request_id(method: str) -> str:
    """RequestId для конверта Head — уникальный на каждый вызов."""
    return f"{method}_{uuid4().hex[:14]}"
