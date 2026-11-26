"""Регресс корзины: добавление, изменение количества, удаление."""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi
from lo_api.constants import SkuKind

pytestmark = pytest.mark.regression


def test_add_item_appears_in_cart(pickup_ready: LentaApi, clean_cart, sku):
    item = sku(SkuKind.REGULAR)

    pickup_ready.cart.add(item.id, 3)
    cart = pickup_ready.cart.lookup()

    assert not cart.is_empty, f"товар {item} не добавился в корзину"
    assert cart.quantity_by_sku.get(item.id) == pytest.approx(3), (
        f"ожидали 3 шт. {item}, в корзине: {cart.quantity_by_sku}"
    )


def test_change_quantity(pickup_ready: LentaApi, clean_cart, sku):
    item = sku(SkuKind.REGULAR)
    pickup_ready.cart.add(item.id, 1)

    pickup_ready.cart.modify_item(item.id, 5)
    cart = pickup_ready.cart.lookup()

    assert cart.quantity_by_sku.get(item.id) == pytest.approx(5)


def test_remove_item(pickup_ready: LentaApi, clean_cart, sku):
    item = sku(SkuKind.REGULAR)
    pickup_ready.cart.add(item.id, 2)

    pickup_ready.cart.remove(item.id)
    cart = pickup_ready.cart.lookup()

    assert item.id not in cart.quantity_by_sku, (
        f"товар {item} остался в корзине после удаления: {cart.quantity_by_sku}"
    )


def test_cart_total_grows_with_items(pickup_ready: LentaApi, clean_cart, sku):
    item = sku(SkuKind.REGULAR)
    before = pickup_ready.cart.lookup()

    pickup_ready.cart.add(item.id, 2)
    after = pickup_ready.cart.lookup()

    if before.total is None or after.total is None:
        pytest.skip("cartLookup не возвращает Total на этом стенде")
    assert after.total > before.total, (
        f"сумма корзины не выросла после добавления товара: {before.total} → {after.total}"
    )
