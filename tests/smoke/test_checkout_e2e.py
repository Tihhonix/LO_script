"""Smoke: полный путь оформления заказа для обеих миссий.

То, чем был весь старый скрипт — но с ассертами, без input() и без хардкодов.
"""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi
from lo_api.constants import AddressKind, Mission, SkuKind
from lo_api.flows import CheckoutFlow, CheckoutParams

pytestmark = [pytest.mark.smoke, pytest.mark.creates_order]


@pytest.mark.parametrize(
    "mission",
    [
        pytest.param(Mission.DELIVERY, marks=pytest.mark.delivery, id="доставка"),
        pytest.param(Mission.PICKUP, marks=pytest.mark.pickup, id="самовывоз"),
    ],
)
def test_checkout_creates_order(
    flow: CheckoutFlow,
    authorized_api: LentaApi,
    clean_cart,
    account,
    sku,
    mission: Mission,
):
    item = sku(SkuKind.REGULAR)
    quantity = 2
    params = CheckoutParams(
        mission=mission,
        items=[(item.id, quantity)],
        address_kind=AddressKind.REGULAR if mission is Mission.DELIVERY else None,
    )

    result = flow.run(account.phone, params)

    # --- организация ---------------------------------------------------
    assert result.organization is not None
    assert authorized_api.ctx.legal_entity_id == result.organization.legal_entity_id, (
        "x-organization-id в контексте разошёлся с выбранной организацией"
    )

    # --- адрес / точка --------------------------------------------------
    if mission is Mission.DELIVERY:
        assert result.address is not None, "для доставки должен быть выбран адрес"
        assert result.address.kind is AddressKind.REGULAR
        assert authorized_api.ctx.selected_address_id == result.address.id
    else:
        assert result.address is None
        assert authorized_api.ctx.store_id is not None, "для самовывоза не выбран магазин"

    # --- корзина --------------------------------------------------------
    cart = result.cart_before_order
    assert cart is not None and not cart.is_empty, "корзина пуста перед созданием заказа"
    assert cart.quantity_by_sku.get(item.id) == pytest.approx(quantity), (
        f"товар {item} не добавился или количество не совпало: {cart.quantity_by_sku}"
    )

    # --- оплата ---------------------------------------------------------
    assert result.payment_types, "список способов оплаты пуст"
    assert authorized_api.payment.find(result.payment_types, result.payment_type_id), (
        f"выбранный способ оплаты {result.payment_type_id} отсутствует в списке"
    )

    # --- интервал -------------------------------------------------------
    assert result.interval is not None
    assert result.interval.is_available, "выбран недоступный интервал"
    assert str(cart.interval_id or result.interval.id) == str(result.interval.id), (
        f"в корзине интервал {cart.interval_id}, а выбирали {result.interval.id}"
    )

    # --- заказ ----------------------------------------------------------
    assert result.pending_key, "orderAdd не вернул PendingRequestKey"
    assert result.order is not None and str(result.order.id).strip(), "у заказа нет Id"
    if result.order.total is not None and cart.total is not None:
        assert result.order.total == pytest.approx(cart.total, rel=0.01), (
            f"сумма заказа {result.order.total} не сошлась с корзиной {cart.total}"
        )


@pytest.mark.delivery
def test_checkout_delivery_stops_before_order(
    flow: CheckoutFlow, clean_cart, account, sku
):
    """Подготовка заказа без его создания — не плодит заказы на стенде."""
    item = sku(SkuKind.REGULAR)
    result = flow.run(
        account.phone,
        CheckoutParams(
            mission=Mission.DELIVERY,
            items=[(item.id, 1)],
            address_kind=AddressKind.REGULAR,
        ),
        place_order=False,
    )
    assert result.order is None
    assert result.interval is not None
    assert result.cart_before_order and not result.cart_before_order.is_empty
