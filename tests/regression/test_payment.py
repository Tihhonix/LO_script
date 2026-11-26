"""Регресс способов оплаты."""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi

pytestmark = pytest.mark.regression


def test_payment_types_available(pickup_ready: LentaApi):
    types, _ = pickup_ready.payment.search()

    assert types, "paymentTypeSearch вернул пустой PaymentTypeList"
    for pt in types:
        assert str(pt.id).strip(), f"у способа оплаты {pt.name!r} пустой Id"
        assert pt.name, f"у способа оплаты Id={pt.id} нет названия"


def test_default_payment_type_present_in_list(pickup_ready: LentaApi):
    """Регресс на найденный баг.

    Старый код сравнивал `pt["Id"] == str(default["typeId"])` без приведения
    левой части: при int в Id совпадение не наступало никогда, и способ по
    умолчанию молча не находился.
    """
    types, default_id = pickup_ready.payment.search()
    if default_id is None:
        pytest.skip("DefaultPaymentType отсутствует в ответе для этого аккаунта")

    found = pickup_ready.payment.find(types, default_id)

    assert found is not None, (
        f"DefaultPaymentType {default_id!r} отсутствует в PaymentTypeList "
        f"{[str(p.id) for p in types]}"
    )
