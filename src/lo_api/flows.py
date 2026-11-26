"""Оркестрация флоу оформления заказа.

Флоу знает, В КАКОМ ПОРЯДКЕ звать клиентов. Он не содержит ассертов и ничего
не печатает — только возвращает подробный результат со всеми промежуточными
снимками, чтобы тест мог проверять не только финал, но и швы между шагами.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lo_api.clients import LentaApi
from lo_api.constants import PAYMENT_EXCLUDED, AddressKind, Mission
from lo_api.http.errors import LoApiError
from lo_api.models import (
    Address,
    Cart,
    Interval,
    Order,
    Organization,
    PastOrder,
    PaymentType,
    StateRegulation,
)

log = logging.getLogger("lo_api.flows")


@dataclass
class CheckoutParams:
    """Параметры сценария — то, что раньше спрашивалось через input()."""

    mission: Mission
    items: list[tuple[str, float]]  # [(sku, quantity), ...]
    address_kind: AddressKind | None = None  # только для доставки
    address_id: int | None = None  # конкретный адрес; None → первый нужного типа
    store_id: int | None = None  # только для самовывоза
    payment_type_id: str | None = None  # None → способ по умолчанию
    interval_index: int = 0  # индекс среди ДОСТУПНЫХ интервалов
    address_vsd: str | None = None  # реальный ВСД для Меркурий-адресов
    partner_card: str | None = None
    buyer_note: str = ""  # комментарий к заказу; браузер шлёт его и пустым
    # Собственные нужды. None — оставить как есть; признак, который стенд
    # менять не разрешает, игнорируется
    vet_own_needs: bool | None = None
    mark_own_needs: bool | None = None


@dataclass
class CheckoutResult:
    """Все промежуточные состояния, а не только номер заказа."""

    organization: Organization | None = None
    address: Address | None = None
    payment_types: list[PaymentType] = field(default_factory=list)
    default_payment_id: str | None = None
    payment_type_id: str | None = None
    intervals: list[Interval] = field(default_factory=list)
    interval: Interval | None = None
    cart_after_items: Cart | None = None
    cart_before_order: Cart | None = None
    pending_key: str | None = None
    order: Order | None = None


def reorder_issues(past: PastOrder, cart: Cart) -> list[str]:
    """Чем корзина отличается от повторяемого заказа. Пусто — совпадает.

    Сравнивается число единиц: у весового это порции (`QuantityOrigin`), а не
    килограммы — корзина хранит именно их. SKU сверяются без ведущих нулей:
    в заказе и корзине они бывают записаны по-разному.
    """
    def norm(sku: str) -> str:
        return sku.lstrip("0") or "0"

    want = {norm(i.sku): (i.units, i) for i in past.items}
    got = {norm(sku): qty for sku, qty in cart.quantity_by_sku.items()}
    issues = []
    for sku, (units, item) in want.items():
        if sku not in got:
            issues.append(f"{item.sku} {item.name}: в корзину не попал")
        elif abs(got[sku] - units) > 1e-9:
            issues.append(f"{item.sku} {item.name}: в корзине {got[sku]:g}, в заказе {units:g}")
    for sku in sorted(got.keys() - want.keys()):
        issues.append(f"{sku}: в корзине, но не было в заказе")
    return issues


class CheckoutFlow:
    def __init__(self, api: LentaApi) -> None:
        self.api = api

    # -- отдельные фазы ----------------------------------------------------
    def authorize(self, phone: str, code: str | None = None) -> Organization:
        """Логин + выбор первой доступной организации. Идемпотентно.

        Если контекст уже авторизован, повторный login не выполняется: стенд
        ограничивает частоту запросов кода, и повторные логины в каждом тесте
        валили прогон с «Вы уже запрашивали код для входа».
        """
        api = self.api
        if not api.ctx.authorized:
            api.auth.login(phone, code)
        orgs = api.b2b.organizations()
        if not orgs:
            raise LoApiError("legalEntity/person",
                             f"у аккаунта {phone} нет доступных организаций")
        if api.ctx.legal_entity_id != orgs[0].legal_entity_id:
            api.b2b.select(orgs[0])
        return orgs[0]

    def set_mission(self, params: CheckoutParams) -> Address | None:
        """Переключение миссии.

        Для доставки: сначала находим адрес нужного типа, выбираем его, и только
        потом переключаем режим — с реальным addressId, а не с захардкоженным.
        """
        api = self.api
        if params.mission is Mission.DELIVERY:
            if params.address_id is not None:
                address = self._address_by_id(params.address_id)
            else:
                kind = params.address_kind or AddressKind.REGULAR
                candidates = api.address.of_kind(kind)
                if not candidates:
                    raise LoApiError("users/address",
                                     f"нет ни одного адреса типа {kind} у этого аккаунта")
                address = candidates[0]
            api.address.select(address)
            api.delivery.set_mode(Mission.DELIVERY, address_id=address.id)
            return address

        api.delivery.set_mode(Mission.PICKUP, store_id=params.store_id)
        return None

    def _address_by_id(self, address_id: int) -> Address:
        known = self.api.address.search()
        found = next((a for a in known if a.id == address_id), None)
        if found is None:
            raise LoApiError(
                "users/address",
                f"адрес {address_id} не найден у этого аккаунта. Доступны: "
                + "; ".join(f"{a.id} — {a}" for a in known),
            )
        return found

    def fill_cart(self, items: list[tuple[str, float]]) -> Cart:
        for sku, qty in items:
            self.api.cart.add(sku, qty)
        return self.api.cart.lookup()

    def cart_from_order(self, past: PastOrder) -> tuple[Cart, list[str]]:
        """Корзина по прошлому заказу — как «Повторить заказ» в веб-морде.

        Корзина сначала чистится: иначе к товарам заказа примешались бы
        остатки прошлого прогона, и «идентичный» заказ вышел бы другим.
        Состав сверяется с заказом — отдать решение, оформлять ли то, что
        получилось, должен человек, а не молчаливый cartFromOrderAdd.
        """
        self.api.cart.clear()
        self.api.cart.from_order(past.id)
        # В ответе cartFromOrderAdd позиций нет (Goods: false) — браузер тоже
        # следом читает корзину
        cart = self.api.cart.lookup()
        return cart, reorder_issues(past, cart)

    def set_own_needs(self, params: CheckoutParams) -> StateRegulation:
        """Признаки собственных нужд перед чекаутом.

        Меняется только то, что стенд разрешает: `abilityVetOwnNeeds` и
        `abilityMarkOwnNeeds` вместе с парными `*ReadOnly`. Если ничего не
        просили или просимое совпадает с текущим, лишний proStateRegulationModify
        не отправляется.
        """
        state = self.api.b2b.state_regulation()
        vet = params.vet_own_needs if (params.vet_own_needs is not None
                                       and state.can_change("vet")) else state.is_vet
        mark = params.mark_own_needs if (params.mark_own_needs is not None
                                         and state.can_change("mark")) else state.is_mark
        if (vet, mark) == (state.is_vet, state.is_mark):
            return state
        return self.api.b2b.set_state_regulation(vet=vet, mark=mark)

    def choose_payment(self, requested: str | None) -> tuple[list[PaymentType], str | None, str]:
        """Способы оплаты, применимые к текущей миссии.

        Состав списка от миссии зависит (в самовывозе нет оплаты сохранённой
        картой, зато есть «При получении»), но кое-что стенд отдаёт лишним —
        это отсекается по PAYMENT_EXCLUDED.
        """
        mission = self.api.ctx.mission
        excluded = PAYMENT_EXCLUDED[mission]
        all_types, default_id = self.api.payment.search()
        types = [p for p in all_types if str(p.id) not in excluded]
        if not types:
            raise LoApiError("paymentTypeSearch", "список способов оплаты пуст")

        if requested and str(requested) in excluded:
            raise LoApiError("paymentTypeSearch",
                             f"способ оплаты {requested!r} неприменим к миссии "
                             f"{mission}. Доступны: {[str(p.id) for p in types]}")
        # Способ по умолчанию тоже может оказаться отсечённым
        if default_id is not None and str(default_id) in excluded:
            default_id = None

        chosen = requested or default_id or str(types[0].id)
        if self.api.payment.find(types, chosen) is None:
            raise LoApiError("paymentTypeSearch",
                             f"способ оплаты {chosen!r} отсутствует в списке "
                             f"{[str(p.id) for p in types]}")
        return types, default_id, chosen

    def choose_interval(self, index: int = 0, payment_type_id: str | int | None = None
                        ) -> tuple[list[Interval], Interval]:
        """Интервалы запрашиваются ПОСЛЕ выбора оплаты.

        Браузер передаёт PaymentTypeId вместе с запросом интервалов — состав
        доступных слотов от способа оплаты зависит.
        """
        intervals = self.api.delivery.intervals(payment_type_id=payment_type_id)
        chosen = self.api.delivery.pick_interval(intervals, index)
        self.api.cart.set_interval(chosen)
        return intervals, chosen

    def place_order(self, params: CheckoutParams, payment_type_id: str) -> tuple[str, Order]:
        self.api.order.save_buyer_note(params.buyer_note)
        # Карта берётся у самой организации, а не из конфига: она своя у каждой,
        # и захардкоженная чужая карта молча ломала бы прогон под другим аккаунтом.
        pending = self.api.order.add(
            payment_type_id,
            address_vsd=params.address_vsd,
            partner_card=params.partner_card or self.api.b2b.card_number(),
        )
        return pending, self.api.order.wait_for_order(pending)

    # -- полный сценарий ---------------------------------------------------
    def run(self, phone: str, params: CheckoutParams, *,
            code: str | None = None, place_order: bool = True) -> CheckoutResult:
        """Полный путь до созданного заказа.

        place_order=False останавливает флоу перед orderAdd — для тестов,
        которые проверяют подготовку корзины и не должны плодить заказы.
        """
        r = CheckoutResult()
        r.organization = self.authorize(phone, code)
        r.address = self.set_mission(params)
        r.cart_after_items = self.fill_cart(params.items)
        self.set_own_needs(params)
        r.payment_types, r.default_payment_id, r.payment_type_id = self.choose_payment(
            params.payment_type_id
        )
        r.intervals, r.interval = self.choose_interval(
            params.interval_index, payment_type_id=r.payment_type_id
        )
        r.cart_before_order = self.api.cart.lookup()

        if place_order:
            r.pending_key, r.order = self.place_order(params, r.payment_type_id)
            log.info("Сценарий завершён, заказ %s", r.order.id)
        return r
