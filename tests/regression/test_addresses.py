"""Регресс адресов: типы, выбор, проверка зоны."""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi
from lo_api.constants import ADDRESS_FLAGS, AddressKind

pytestmark = [pytest.mark.regression, pytest.mark.delivery]


def test_address_list_not_empty(authorized_api: LentaApi, account):
    addresses = authorized_api.address.search()

    assert addresses, f"у аккаунта {account} нет ни одного адреса"
    for a in addresses:
        assert a.id, f"у адреса {a.street} {a.house} нет Id"


@pytest.mark.parametrize("kind", list(AddressKind), ids=lambda k: str(k))
def test_address_kind_filter_is_consistent(authorized_api: LentaApi, kind: AddressKind):
    """Фильтр по флагам, а не по названию города.

    Заодно фиксирует, что флаги isAddressMercury / isAddressEgais
    взаимоисключающи — на них завязан выбор сценария.
    """
    found = authorized_api.address.of_kind(kind)
    if not found:
        pytest.skip(f"у аккаунта нет адресов типа {kind}")

    expected_mercury, expected_egais = ADDRESS_FLAGS[kind]
    for a in found:
        assert a.is_mercury is expected_mercury
        assert a.is_egais is expected_egais
        assert a.kind is kind


def test_select_address_persists(authorized_api: LentaApi):
    addresses = authorized_api.address.of_kind(AddressKind.REGULAR)
    if not addresses:
        pytest.skip("у аккаунта нет обычных адресов")
    target = addresses[0]

    authorized_api.address.select(target)

    assert authorized_api.ctx.selected_address_id == target.id


def test_address_check_uses_selected_address(authorized_api: LentaApi):
    """Регресс на найденный баг: координаты были захардкожены и не связаны
    с выбранным адресом — проверка зоны шла вхолостую."""
    addresses = [a for a in authorized_api.address.of_kind(AddressKind.REGULAR)
                 if a.latitude is not None and a.longitude is not None]
    if not addresses:
        pytest.skip("userAddressSearch не отдаёт координаты адресов")
    target = addresses[0]
    authorized_api.address.select(target)

    payload = authorized_api.address.check(target)

    assert payload.get("Body"), f"userAddressCheck вернул пустой Body для {target}"
