"""Регресс интервалов доставки и самовывоза."""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi
from lo_api.constants import AddressKind, Mission

pytestmark = pytest.mark.regression


@pytest.mark.pickup
def test_pickup_intervals_available(pickup_ready: LentaApi):
    intervals = pickup_ready.delivery.intervals()

    assert intervals, "для самовывоза не вернулось ни одного интервала"
    assert any(i.is_available for i in intervals), (
        f"все {len(intervals)} интервалов помечены недоступными"
    )
    for i in intervals:
        assert str(i.id).strip(), f"у интервала {i.title!r} пустой Id"


@pytest.mark.delivery
def test_delivery_intervals_available(authorized_api: LentaApi):
    addresses = authorized_api.address.of_kind(AddressKind.REGULAR)
    if not addresses:
        pytest.skip("у аккаунта нет обычных адресов")
    address = addresses[0]
    authorized_api.address.select(address)
    authorized_api.delivery.set_mode(Mission.DELIVERY, address_id=address.id)

    intervals = authorized_api.delivery.intervals()

    assert intervals, f"для адреса {address} не вернулось ни одного интервала"
    assert any(i.is_available for i in intervals)


@pytest.mark.pickup
def test_selected_interval_lands_in_cart(pickup_ready: LentaApi, clean_cart, sku):
    """Проверка шва между шагами: cartModify должен применить интервал.

    Старый скрипт передавал интервал и никогда не проверял, что он применился.
    """
    from lo_api.constants import SkuKind

    item = sku(SkuKind.REGULAR)
    pickup_ready.cart.add(item.id, 1)
    intervals = pickup_ready.delivery.intervals()
    chosen = pickup_ready.delivery.pick_interval(intervals, 0)

    pickup_ready.cart.set_interval(chosen)
    cart = pickup_ready.cart.lookup()

    if cart.interval_id is None:
        pytest.skip("cartLookup не отдаёт DeliveryIntervalId на этом стенде")
    assert str(cart.interval_id) == str(chosen.id), (
        f"в корзине интервал {cart.interval_id}, а применяли {chosen.id}"
    )
