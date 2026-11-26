"""Pydantic-модели ответов.

Зафиксированный контракт вместо гадания вида
    verify_json.get("token") or verify_json.get("Body", {}).get("SessionToken")

Модели намеренно узкие: только поля, которые используются в ассертах.
extra="allow" — чтобы новое поле на бэкенде не ломало тесты на ровном месте.
Если появится Swagger, их можно будет заменить сгенерированными.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lo_api.constants import AddressKind


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class VerifyResult(_Base):
    """Ответ verify. Схема подтверждена HAR:
    {isB2B, isNewUser, jwt:{access,refresh}, passportId, token}."""

    token: str = Field(min_length=8)
    passport_id: str = Field(alias="passportId")
    is_b2b: bool = Field(default=False, alias="isB2B")
    is_new_user: bool = Field(default=False, alias="isNewUser")


class Organization(_Base):
    legal_entity_id: str = Field(alias="legalEntityId")
    name: str
    inn: str = ""
    kpp: str = ""
    status: str = ""

    def __str__(self) -> str:
        return f"{self.name} (ИНН {self.inn}, {self.status})"


class LegalEntityDetails(_Base):
    """Реквизиты ВЫБРАННОЙ организации.

    Единственный источник карты лояльности (`proCardNumber`), без которой
    `orderAdd` отклоняет заказ. Прописывать её в конфиг нельзя: она своя у
    каждой организации, а прогон должен работать с любым аккаунтом.
    """

    legal_entity_id: str = Field(default="", alias="legalEntityId")
    name: str = ""
    inn: str = ""
    pro_card_number: str = Field(default="", alias="proCardNumber")
    mercury: bool = False
    egais: bool = False

    def __str__(self) -> str:
        return f"{self.name}, карта {self.pro_card_number or '—'}"


class Address(_Base):
    id: int = Field(alias="Id")
    town: str = Field(default="", alias="TownTitle")
    street: str = Field(default="", alias="StreetTitle")
    house: str = Field(default="", alias="HouseNumber")
    is_mercury: bool = Field(default=False, alias="isAddressMercury")
    is_egais: bool = Field(default=False, alias="isAddressEgais")
    # Координаты приходят под яндексовыми именами и строками, а не Latitude/Longitude
    latitude: float | None = Field(default=None, alias="YandexLatitude")
    longitude: float | None = Field(default=None, alias="YandexLongitude")
    selected: bool = Field(default=False, alias="Selected")

    @property
    def kind(self) -> AddressKind:
        if self.is_mercury and not self.is_egais:
            return AddressKind.MERCURY
        if self.is_egais and not self.is_mercury:
            return AddressKind.EGAIS
        return AddressKind.REGULAR

    def __str__(self) -> str:
        return f"{self.town}, {self.street} {self.house} (Id: {self.id}, {self.kind})"


class PastOrderItem(_Base):
    """Позиция заказа из orderLookup.

    У весового `Quantity` — килограммы (2.1), а `QuantityOrigin` — число
    порций (3 по 700 г, см. `Numerator`/`Denominator`). В корзину кладутся
    порции, поэтому для сверки с корзиной нужно `QuantityOrigin`.
    """

    sku: str = Field(alias="Id")
    name: str = Field(default="", alias="Name")
    quantity: float = Field(default=0, alias="Quantity")
    units: float = Field(default=0, alias="QuantityOrigin")
    weight_flag: str | int | bool = Field(default="0", alias="FlagWeightGood")

    @property
    def is_weight(self) -> bool:
        return str(self.weight_flag) in ("1", "True", "true")

    def __str__(self) -> str:
        return (f"{self.sku} {self.name} — {self.units:g} порц. ({self.quantity:g} кг)"
                if self.is_weight else f"{self.sku} {self.name} ×{self.units:g}")


class PastOrder(_Base):
    """Оформленный заказ глазами покупателя — orderLookup.

    Всё, что нужно, чтобы оформить такой же: миссия, адрес или магазин,
    оплата, собственные нужды и время. Организации в ответе нет.
    """

    id: str = Field(alias="Id")
    delivery_type: str = Field(default="", alias="DeliveryType")
    address_id: int | None = Field(default=None, alias="DeliveryAddressId")
    store_id: int | None = Field(default=None, alias="DeliveryStoreId")
    payment_type_id: str | int | None = Field(default=None, alias="PaymentTypeId")
    date: str = Field(default="", alias="DeliveryDate")
    time_from: str = Field(default="", alias="DeliveryTimeFrom")
    status_text: str = Field(default="", alias="StatusText")
    items: list[PastOrderItem] = Field(default_factory=list, alias="OrderItemList")
    pro: dict = Field(default_factory=dict, alias="Pro")

    @property
    def is_pickup(self) -> bool:
        return self.delivery_type == "pickup"

    @property
    def vet_own_needs(self) -> bool | None:
        return self.pro.get("vetOwnNeeds")

    @property
    def mark_own_needs(self) -> bool | None:
        return self.pro.get("markOwnNeeds")

    def summary(self) -> str:
        where = (f"самовывоз, магазин {self.store_id}" if self.is_pickup
                 else f"доставка, адрес {self.address_id}")
        return f"{where}; оплата {self.payment_type_id}; {self.date} {self.time_from[:5]}"


class StateRegulation(_Base):
    """Ответ proStateRegulationLookup: собственные нужды по корзине.

    `ability*` — можно ли вообще переключать признак, `is*` — как он стоит
    сейчас, `has*Goods` — есть ли в корзине такие товары. Отдельный
    `*ReadOnly` запрещает правку даже там, где ability разрешает.
    """

    ability_vet: bool = Field(default=False, alias="abilityVetOwnNeeds")
    ability_mark: bool = Field(default=False, alias="abilityMarkOwnNeeds")
    vet_read_only: bool = Field(default=False, alias="abilityVetOwnNeedsReadOnly")
    mark_read_only: bool = Field(default=False, alias="abilityMarkOwnNeedsReadOnly")
    is_vet: bool = Field(default=False, alias="isVetOwnNeeds")
    is_mark: bool = Field(default=False, alias="isMarkOwnNeeds")
    has_vet_goods: bool = Field(default=False, alias="hasVetGoods")
    has_mark_goods: bool = Field(default=False, alias="hasMarkGoods")
    message: str = Field(default="", alias="messageOwnNeeds")

    @field_validator("message", mode="before")
    @classmethod
    def _none_is_empty(cls, v: object) -> object:
        """Стенд присылает messageOwnNeeds: null, когда сообщения нет."""
        return "" if v is None else v

    def can_change(self, kind: str) -> bool:
        """Можно ли переключить признак: `vet` или `mark`."""
        if kind == "vet":
            return self.ability_vet and not self.vet_read_only
        return self.ability_mark and not self.mark_read_only

    def enabled(self, kind: str) -> bool:
        return self.is_vet if kind == "vet" else self.is_mark

    def __str__(self) -> str:
        def part(title: str, kind: str) -> str:
            state = "да" if self.enabled(kind) else "нет"
            return f"{title} — {state}" + ("" if self.can_change(kind) else ", менять нельзя")
        return f"{part('ветеринарные', 'vet')}; {part('маркированные', 'mark')}"


class Store(_Base):
    """Магазин самовывоза из stores/pickup/search.

    `id` здесь — это тот самый storeId, который уходит в deliveryModeSet и
    DeliveryStoreId заказа. `alias` — код вроде «0012», `title` — «ТК12».
    """

    id: int
    title: str = ""
    alias: str = ""
    address: str = Field(default="", alias="addressShort")
    address_full: str = Field(default="", alias="addressFull")
    market_type: str = Field(default="", alias="marketType")
    selected: bool = False

    def __str__(self) -> str:
        return f"{self.title} — {self.address} (Id: {self.id})"


class CartGood(_Base):
    """Позиция корзины из CartList[0].Goods.

    SKU здесь приходит под именем GoodsItemId, а не Id: под Id в том же ответе
    лежит идентификатор строки корзины. Название — Name, не Title.
    """

    id: str = Field(alias="GoodsItemId")
    title: str = Field(default="", alias="Name")
    quantity: float = Field(default=0, alias="Quantity")
    price: float | None = Field(default=None, alias="Price")


class Cart(_Base):
    """Корзина. Лежит в Body.CartList[0], а не в Body напрямую (подтверждено HAR).

    Позиции — в CartList[0].Goods. Раньше они искались по ключам, которых в
    ответе нет, и корзина всегда выглядела пустой по составу.
    """

    goods: list[CartGood] = Field(default_factory=list, alias="Goods")
    total: float | None = Field(default=None, alias="TotalCost")
    base_total: float | None = Field(default=None, alias="BaseTotalCost")
    positions_count: int | None = Field(default=None, alias="PositionsCount")
    interval_id: str | int | None = Field(default=None, alias="DeliveryIntervalId")
    delivery_type: str | None = Field(default=None, alias="DeliveryType")
    store_id: int | None = Field(default=None, alias="DeliveryStoreId")
    address_id: int | None = Field(default=None, alias="UserAddressId")
    error_code: str | int | None = Field(default=None, alias="ErrorCode")

    @classmethod
    def from_body(cls, body: dict) -> Cart:
        """Собирает корзину из Body ответа cartLookup / cartItemModify."""
        carts = body.get("CartList") or []
        return cls.model_validate(dict(carts[0]) if carts else {})

    @property
    def quantity_by_sku(self) -> dict[str, float]:
        return {g.id: g.quantity for g in self.goods}

    @property
    def is_empty(self) -> bool:
        """Пустая ли корзина.

        Позиции надёжнее считать по PositionsCount: список товаров приходит
        не во всех ответах.
        """
        if self.positions_count is not None:
            return self.positions_count == 0
        if self.goods:
            return False
        return not self.total


class OrganizationBalance(_Base):
    """Баланс организации. Нужен для оплаты «Списать с баланса»."""

    balance: float | None = Field(default=None, alias="Balance")
    credit_limit: float | None = Field(default=None, alias="CreditLimit")
    hold: float | None = Field(default=None, alias="HoldAmount")
    is_owe: bool = Field(default=False, alias="IsOwe")
    updated: str = Field(default="", alias="Updated")

    def __str__(self) -> str:
        return f"{self.balance}, в холде {self.hold}" + (", ДОЛГ" if self.is_owe else "")


class PaymentType(_Base):
    id: str | int = Field(alias="Id")
    name: str = Field(default="", alias="Name")
    description: str = Field(default="", alias="Description")
    active: bool = Field(default=True, alias="Active")
    price_limit: float | None = Field(default=None, alias="PriceLimit")

    def __str__(self) -> str:
        return f"{self.name} (Id: {self.id})"


class Interval(_Base):
    """Интервал доставки/самовывоза.

    Доступность приходит как Disabled (а не IsAvailable) — подтверждено HAR.
    """

    id: str | int = Field(alias="Id")
    title: str = Field(default="", alias="Title")
    date: str | None = Field(default=None, alias="Date")
    time_from: str | None = Field(default=None, alias="TimeFrom")
    time_to: str | None = Field(default=None, alias="TimeTo")
    price: float | None = Field(default=None, alias="Price")
    disabled: bool = Field(default=False, alias="Disabled")
    alcohol_enabled: bool = Field(default=True, alias="AlcoholEnabled")

    @property
    def is_available(self) -> bool:
        return not self.disabled

    def __str__(self) -> str:
        return self.title or f"{self.date} {self.time_from}-{self.time_to} (Id: {self.id})"


class Order(_Base):
    id: str | int = Field(alias="Id")
    status_text: str = Field(default="", alias="StatusText")
    status_code: int | None = Field(default=None, alias="StatusCode")
    group_status: str = Field(default="", alias="GroupStatus")
    total: float | None = Field(default=None, alias="TotalCost")
    goods_cost: float | None = Field(default=None, alias="GoodsCost")
    payment_type_id: str | int | None = Field(default=None, alias="PaymentTypeId")
    interval_id: str | None = Field(default=None, alias="IntervalId")
    delivery_type: str = Field(default="", alias="DeliveryType")
    delivery_time: str = Field(default="", alias="DeliveryTimeTitle")

    def __str__(self) -> str:
        return f"№{self.id} — {self.status_text or self.group_status}, {self.total}"
