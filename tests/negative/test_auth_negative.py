"""Негативные проверки авторизации.

Здесь важно, что клиенты поднимают исключения, а не молча едут дальше со
значением None — именно так вёл себя старый скрипт.

Используется `fresh_api` — отдельный неавторизованный контекст, чтобы эти
проверки не портили общую сессию остальных тестов.
"""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi
from lo_api.http.errors import LoApiError

pytestmark = [pytest.mark.regression, pytest.mark.negative]


def test_wrong_code_rejected(fresh_api: LentaApi, account):
    # request_code терпим к «код уже запрошен» — лимит стенда это не ошибка теста
    fresh_api.auth.request_code(account.phone)

    with pytest.raises(LoApiError) as exc:
        fresh_api.auth.verify_code(account.phone, "0000")

    assert fresh_api.ctx.session_token is None, (
        "после неудачного verify токен не должен появляться"
    )
    assert exc.value.trace_id, "в ошибке нет x-trace-id — нечем искать запрос в трассировке"


@pytest.mark.parametrize("phone", ["123", "abcdefghij", ""], ids=["короткий", "буквы", "пустой"])
def test_invalid_phone_rejected(fresh_api: LentaApi, phone: str):
    with pytest.raises(LoApiError):
        fresh_api.auth.send_code(phone)


def test_anonymous_session_gets_no_user_addresses(fresh_api: LentaApi):
    """Анонимная сессия не должна видеть адреса пользователя.

    Формулировка намеренно не привязана к коду ответа: корректны оба поведения —
    и отказ (401/403), и пустой список. Тест падает только на реальной утечке,
    поэтому не сломается, если API сменит способ отказа.

    Раньше здесь стояла проверка «cartLookup без токена должен упасть». Она
    оказалась неверной: см. test_cart_lookup_without_token_is_guest_cart.
    """
    assert fresh_api.ctx.session_token is None

    try:
        addresses = fresh_api.address.search()
    except LoApiError:
        return  # API отвергает анонимный запрос — это ожидаемое поведение

    assert not addresses, (
        "анонимная сессия получила список адресов пользователя: "
        f"{[str(a) for a in addresses]}"
    )


def test_anonymous_session_gets_no_profile(fresh_api: LentaApi):
    """То же для профиля: либо отказ, либо ответ без данных пользователя."""
    try:
        profile = fresh_api.user.profile()
    except LoApiError:
        return

    leaked = {k: v for k, v in (profile or {}).items()
              if k.lower() in {"phone", "passportid", "email", "firstname", "lastname"} and v}
    assert not leaked, f"анонимная сессия получила данные пользователя: {sorted(leaked)}"


def test_cart_lookup_without_token_is_guest_cart(fresh_api: LentaApi):
    """Зафиксированная особенность стенда, а не проверка отказа.

    Обнаружено при первом прогоне: cartLookup без Head.SessionToken отвечает
    HTTP 200 и отдаёт ГОСТЕВУЮ корзину, а не ошибку. Практическое следствие:
    если токен потерян посреди флоу, корзинные вызовы не упадут — они молча
    начнут работать с чужой (гостевой) корзиной, и заказ соберётся не тот.

    Поэтому принадлежность корзины проверяется в smoke по составу, а не по
    факту успешного ответа.
    """
    assert fresh_api.ctx.session_token is None

    try:
        cart = fresh_api.cart.lookup()
    except LoApiError:
        pytest.skip("стенд начал отвергать анонимный cartLookup — поведение изменилось")

    assert cart.is_empty, (
        "гостевая корзина у свежего анонимного контекста должна быть пустой, "
        f"получено: {cart.quantity_by_sku}"
    )
