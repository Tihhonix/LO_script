"""Доменные перечисления и мапы.

Всё, что раньше было магическими строками и числами по всему скрипту.
"""

from __future__ import annotations

from enum import StrEnum


class Mission(StrEnum):
    """Миссия заказа."""

    DELIVERY = "delivery"
    PICKUP = "pickup"


class AddressKind(StrEnum):
    """Тип адреса доставки.

    Определяется парой флагов в ответе userAddressSearch:
    isAddressMercury / isAddressEgais.
    """

    REGULAR = "regular"
    MERCURY = "mercury"
    EGAIS = "egais"


class SkuKind(StrEnum):
    """Категория тестового товара.

    Нужна, чтобы сценарий просил «весовой товар», а не конкретный SKU:
    подстановка идёт из конфига (LO_SKUS), а не из тела теста.
    """

    REGULAR = "regular"
    WEIGHT = "weight"
    MERCURY = "mercury"
    ALCOHOL = "alcohol"
    LIMITED = "limited"


# IntervalType в теле userAddressDeliveryIntervalSearchsynced
INTERVAL_TYPE: dict[Mission, str] = {
    Mission.DELIVERY: "planned_pro",
    Mission.PICKUP: "pickup",
}

# Ключ, под которым лежит список интервалов в ответе — разный для миссий
INTERVAL_LIST_KEY: dict[Mission, str] = {
    Mission.DELIVERY: "DeliveryPlannedProIntervalList",
    Mission.PICKUP: "DeliveryPickupIntervalList",
}

# Способы оплаты, которые стенд возвращает, но к миссии они неприменимы.
# «Оплата без привязки карты» (2) описана через доставку — «заморозим сумму
# заказа, а после доставки спишем итоговую», — и при самовывозе предлагаться
# не должна. Стенд её всё равно отдаёт, поэтому отсекаем на нашей стороне.
PAYMENT_EXCLUDED: dict[Mission, frozenset[str]] = {
    Mission.DELIVERY: frozenset(),
    Mission.PICKUP: frozenset({"2"}),
}

# Флаги адреса → AddressKind
ADDRESS_FLAGS: dict[AddressKind, tuple[bool, bool]] = {
    #                 (isAddressMercury, isAddressEgais)
    AddressKind.REGULAR: (False, False),
    AddressKind.MERCURY: (True, False),
    AddressKind.EGAIS: (False, True),
}

CLIENT_VERSION = "angular_web_0.0.2"
