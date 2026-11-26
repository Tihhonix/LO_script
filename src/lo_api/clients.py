"""Клиенты API, сгруппированные по доменам.

Клиент знает, КАК позвать эндпоинт (путь, конверт, параметры) и возвращает
разобранную модель. Он не знает, в каком порядке звать (это флоу) и что
должно получиться (это тест).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from lo_api.constants import (
    ADDRESS_FLAGS,
    INTERVAL_LIST_KEY,
    INTERVAL_TYPE,
    AddressKind,
    Mission,
)
from lo_api.http.errors import BusinessError, HttpStatusError, LoApiError
from lo_api.http.transport import Context, Transport
from lo_api.models import (
    Address,
    Cart,
    Interval,
    LegalEntityDetails,
    Order,
    Organization,
    OrganizationBalance,
    PastOrder,
    PaymentType,
    StateRegulation,
    Store,
    VerifyResult,
)

log = logging.getLogger("lo_api.clients")


class _Client:
    def __init__(self, t: Transport) -> None:
        self.t = t
        self.ctx: Context = t.ctx
        self.cfg = t.cfg


# Маркер ответа «код уже был отправлен и ещё действует».
# Стенд отвечает на это HTTP 500 с ERROR_INTERNAL, поэтому опознаём по тексту:
# другого признака API не даёт.
CODE_ALREADY_SENT_MARKERS = ("уже запрашивали код", "already requested")


def _is_code_already_sent(exc: LoApiError) -> bool:
    """Опознаёт лимит по сообщению, устойчиво к экранированию кириллицы.

    Тело может прийти как UTF-8, так и с \\uXXXX-эскейпами (json.dumps по
    умолчанию, промежуточные прокси), поэтому сначала пробуем разобрать JSON
    и взять message, и только потом падаем на сравнение сырого текста.
    """
    raw = exc.payload if isinstance(exc.payload, str) else str(exc.payload or exc)
    haystacks = [raw]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        haystacks.append(str(parsed.get("message", "")))
    return any(
        marker.lower() in h.lower() for h in haystacks for marker in CODE_ALREADY_SENT_MARKERS
    )


# ---------------------------------------------------------------------------
class AuthClient(_Client):
    """Авторизация по телефону.

    Пути вынесены в конфиг: на стенде send и verify живут по разным префиксам,
    и это расхождение приходится держать настраиваемым.
    """

    def start_guest_session(self) -> str:
        """Заводит гостевую сессию перед запросом кода.

        Подтверждено HAR: браузер зовёт send и verify с уже существующим
        sessiontoken, а verify возвращает ТОТ ЖЕ токен, повысив его до
        авторизованного. Без сессии код некуда привязать, и verify отвечает
        400 BAD_REQUEST — даже на верный код и верный номер.
        """
        payload = self.t.gateway(self.cfg.auth_guest_path,
                                 label="auth/token/guest", retry=False)
        session_id = payload.get("sessionId") or payload.get("SessionId")
        if not session_id:
            raise BusinessError("auth/token/guest",
                                f"в ответе нет sessionId: {str(payload)[:200]}")
        self.ctx.session_token = session_id
        log.info("Гостевая сессия получена: %s", session_id)
        return session_id

    def send_code(self, phone: str, *, expected_status: int | tuple[int, ...] = 200) -> Any:
        return self.t.gateway(
            self.cfg.auth_send_path, label="auth/code/send", expected_status=expected_status,
            # Тело ровно как в браузере (HAR): kfpVn/kfpKsid не передаются
            json_body={"phone": phone, "acceptMarketingCommunications": False},
        )

    def request_code(self, phone: str) -> bool:
        """Запрашивает код, терпимо относясь к «код уже запрошен».

        Стенд ограничивает частоту запросов кода. Для набора тестов это не
        ошибка: код-заглушка уже отправлен и ещё действует, авторизоваться им
        можно. Раньше это валило все тесты после первого.

        Возвращает True, если код отправлен сейчас, и False, если
        переиспользуется ранее отправленный.
        """
        try:
            self.send_code(phone)
            return True
        except LoApiError as exc:
            if _is_code_already_sent(exc):
                log.info("Код для %s уже запрошен ранее — переиспользуем", phone)
                return False
            raise

    def verify_code(self, phone: str, code: str, *,
                    expected_status: int | tuple[int, ...] = 200) -> Any:
        body = {"phone": phone, "code": code}
        try:
            return self.t.gateway(
                self.cfg.auth_verify_path, label="auth/code/verify",
                expected_status=expected_status,
                retry=False,  # неидемпотентно: попытки ввода кода ограничены
                json_body=body,
            )
        except HttpStatusError as exc:
            if not (exc.status == 404 and self.cfg.auth_verify_autofallback):
                raise
            fallback = self.cfg.auth_verify_fallback_path
            log.warning(
                "verify по %s вернул 404 «Page not found» — пробую %s. "
                "Если сработает, зафиксируйте путь: LO_AUTH_VERIFY_PATH=%s",
                self.cfg.auth_verify_path, fallback, fallback,
            )
            payload = self.t.gateway(
                fallback, label="auth/code/verify(fallback)",
                expected_status=expected_status, retry=False,
                json_body=body,
            )
            log.warning("Сработал запасной путь verify: %s. Пропишите его в .env, "
                        "чтобы не тратить попытку ввода кода на каждый прогон.", fallback)
            return payload

    def login(self, phone: str, code: str | None = None) -> VerifyResult:
        """Полная авторизация. Записывает токен и passportId в контекст.

        Код обязателен: подставлять «1234» вслепую нельзя — стенд отвечает на
        неверный код 400 BAD_REQUEST, и это выглядит как поломка клиента.
        """
        code = code or self.cfg.sms_stub_code
        if not code:
            raise LoApiError(
                "auth/code/verify",
                "не задан код входа. Передайте его явно (--code), задайте "
                "LO_SMS_STUB_CODE в .env, либо минуйте авторизацию готовым "
                "токеном (--token / LO_SESSION_TOKEN)",
            )
        if self.cfg.auth_use_guest_session and not self.ctx.session_token:
            self.start_guest_session()
        self.request_code(phone)
        try:
            payload = self.verify_code(phone, code)
        except HttpStatusError as exc:
            if exc.status == 400:
                raise HttpStatusError(
                    "auth/code/verify",
                    f"код {code!r} отвергнут (400 BAD_REQUEST). Возможные причины: "
                    f"код неверен или истёк; номер {phone!r} не заведён на стенде; "
                    f"сессия не привязана (проверьте, что перед send прошёл "
                    f"auth/token/guest). Обходной путь — готовый токен: --token",
                    status=400, payload=exc.payload, trace_id=exc.trace_id,
                ) from exc
            raise
        result = VerifyResult.model_validate(payload)
        self.ctx.session_token = result.token
        self.ctx.passport_id = result.passport_id
        log.info("Авторизован %s: passportId=%s", phone, result.passport_id)
        return result

    def use_token(self, token: str, passport_id: str = "") -> None:
        """Принимает готовый токен, минуя авторизацию целиком."""
        self.ctx.session_token = token
        if passport_id:
            self.ctx.passport_id = passport_id
        log.info("Использую готовый sessionToken (авторизация пропущена)")

    def is_session_alive(self) -> bool:
        """Дешёвая проверка живости токена перед использованием из кэша.

        Сетевая ошибка — не приговор токену: она значит, что стенд недоступен
        (нет VPN, лежит хост). Раньше это считалось протухшей сессией, кэш
        стирался, и следующий прогон зря тратил попытку входа по коду.
        """
        if not self.ctx.session_token:
            return False
        try:
            self.t.gateway("/api-gateway/v1/users/profile",
                           http_method="GET", label="users/profile", retry=False)
        except HttpStatusError as exc:
            if exc.status is None:
                raise
            log.info("Сохранённая сессия недействительна: %s", exc)
            return False
        except LoApiError as exc:
            log.info("Сохранённая сессия недействительна: %s", exc)
            return False
        return True


# ---------------------------------------------------------------------------
class B2bClient(_Client):
    def organizations(self) -> list[Organization]:
        """Список организаций аккаунта.

        Живёт на основном хосте за api-gateway и авторизуется одним
        sessiontoken — заголовок Passport-Id браузер здесь не шлёт.
        """
        payload = self.t.gateway(
            self.cfg.b2b_org_path, http_method="GET", label="legalEntity/person",
        )
        return [Organization.model_validate(o) for o in payload.get("list", [])]

    def select(self, org: Organization) -> dict:
        """Переключает B2B-профиль и запоминает организацию в контексте.

        С этого момента x-organization-id во всех запросах берётся из контекста,
        а не из захардкоженного литерала.
        """
        self.ctx.legal_entity_id = org.legal_entity_id
        payload = self.t.gateway(
            self.cfg.b2b_org_select_path, label="users/profile/select",
            json_body={"LegalEntityId": org.legal_entity_id},
        )
        log.info("Выбрана организация: %s", org)
        return payload

    def details(self) -> LegalEntityDetails:
        """Реквизиты выбранной организации.

        Требует уже выполненного select: до него эндпоинт отвечает 403.
        """
        payload = self.t.gateway(self.cfg.b2b_org_details_path, http_method="GET",
                                 label="b2b/legalEntity")
        return LegalEntityDetails.model_validate(payload)

    def card_number(self) -> str:
        """Карта лояльности организации, «» если её нет.

        Пустая строка — законный ответ: об отсутствии карты сообщит orderAdd,
        который единственный знает, что без неё заказ не создаётся.
        """
        return self.details().pro_card_number

    def balance(self) -> OrganizationBalance:
        """Баланс организации: без него оплата 702 «Списать с баланса» падает
        неинформативным OrderCreationException уже на orderAdd."""
        payload = self.t.jrpc("proOrganizationBalance", {"NeedUpdate": True})
        return OrganizationBalance.model_validate(payload.get("result") or {})

    def state_regulation(self, count: int = 1) -> StateRegulation:
        """Признаки собственных нужд для текущей корзины.

        OrganizationId здесь — не идентификатор организации: браузер шлёт
        литеральную 1 независимо от выбранной компании (подтверждено HAR).
        Подстановка legalEntityId сюда была ошибкой рефакторинга.
        """
        payload = self.t.jrpc("proStateRegulationLookup", {
            "OrganizationId": 1,
            "Count": count,
        })
        return StateRegulation.model_validate(payload.get("result") or {})

    def set_state_regulation(self, *, vet: bool, mark: bool,
                             count: int = 1) -> StateRegulation:
        """Переключает собственные нужды. Оба признака едут вместе.

        Отправлять можно только то, что разрешает lookup: `abilityVetOwnNeeds`
        и `abilityMarkOwnNeeds` (плюс парные `*ReadOnly`). Проверка — на
        вызывающей стороне, здесь только запрос.
        """
        payload = self.t.jrpc("proStateRegulationModify", {
            "OrganizationId": 1,
            "Count": count,
            "isVetOwnNeeds": vet,
            "isMarkOwnNeeds": mark,
        })
        result = StateRegulation.model_validate(payload.get("result") or {})
        log.info("Собственные нужды: %s", result)
        return result


# ---------------------------------------------------------------------------
class UserClient(_Client):
    def profile(self) -> dict:
        return self.t.gateway("/api-gateway/v1/users/profile",
                              http_method="GET", label="users/profile")


# ---------------------------------------------------------------------------
class StoreClient(_Client):
    def pickup(self) -> list[Store]:
        """Магазины, доступные для самовывоза.

        Тело запроса пустое: список зависит только от аккаунта. `id` магазина
        и есть storeId для deliveryModeSet — отдельного сопоставления с
        `alias` («0012») или названием («ТК12») не требуется.
        """
        payload = self.t.gateway(self.cfg.stores_pickup_path, json_body={},
                                 label="stores/pickup/search")
        return [Store.model_validate(item) for item in payload.get("items", [])]


class AddressClient(_Client):
    def search(self) -> list[Address]:
        """Все адреса аккаунта.

        Через api-gateway, а не легаси userAddressSearch: последний отдаёт
        только уже выбранный адрес (TotalCount=1), из-за чего выбор адреса по
        типу был фикцией — фильтр мог совпасть лишь с текущим.
        """
        payload = self.t.gateway("/api-gateway/v1/users/address",
                                 http_method="GET", label="users/address")
        return [Address.model_validate(a) for a in payload.get("UserAddressList", [])]

    def of_kind(self, kind: AddressKind) -> list[Address]:
        """Фильтр по флагам, а не по названию города.

        Старый код фильтровал по `"Санкт-Петербург" in TownTitle` — привязка к
        конкретному городу, ломавшаяся при смене тестовых данных.
        """
        mercury, egais = ADDRESS_FLAGS[kind]
        return [a for a in self.search()
                if a.is_mercury == mercury and a.is_egais == egais]

    def select(self, address: Address) -> dict:
        payload = self.t.rest("userAddressSelectedSet", {"Address": address.id})
        self.ctx.selected_address_id = address.id
        log.info("Выбран адрес: %s", address)
        return payload

    def check(self, address: Address) -> dict:
        """Проверка зоны доставки по координатам ВЫБРАННОГО адреса.

        В старом коде координаты были захардкожены и не связаны с выбранным
        адресом — проверка шла вхолостую.
        """
        if address.latitude is None or address.longitude is None:
            raise LoApiError("userAddressCheck",
                             f"у адреса {address.id} нет координат в ответе userAddressSearch")
        return self.t.rest("userAddressCheck", {
            "Latitude": address.latitude,
            "Longitude": address.longitude,
        })


# ---------------------------------------------------------------------------
class DeliveryClient(_Client):
    def set_mode(self, mission: Mission, *, address_id: int | None = None,
                 store_id: int | None = None) -> Any:
        """Переключение миссии.

        Миссия пишется в контекст ДО запроса, поэтому MPK и x-delivery-mode
        в этом же вызове уже соответствуют новой миссии. В старом коде
        deliveryModeSet выполнялся до выбора миссии, с чужим адресом.
        """
        self.ctx.mission = mission
        body: dict[str, Any] = {"type": str(mission)}
        if mission is Mission.DELIVERY:
            if address_id is None:
                raise LoApiError("delivery/mode/set", "для доставки нужен addressId")
            body["addressId"] = address_id
            self.ctx.selected_address_id = address_id
        else:
            body["addressId"] = None
            body["storeId"] = store_id or self.cfg.pickup_store_id
            self.ctx.store_id = body["storeId"]
        payload = self.t.gateway("/api-gateway/v1/delivery/mode/set",
                                 label="delivery/mode/set", json_body=body)
        log.info("Режим доставки: %s (%s)", mission, body)
        return payload

    def intervals(self, mission: Mission | None = None, *,
                  payment_type_id: str | int | None = None) -> list[Interval]:
        """Список интервалов.

        PaymentTypeId браузер передаёт вместе с запросом интервалов — состав
        слотов от способа оплаты зависит. Поэтому оплата выбирается ДО интервалов.
        """
        mission = mission or self.ctx.mission
        if mission is Mission.DELIVERY:
            body = {
                "IntervalType": INTERVAL_TYPE[mission],
                "UserAddressId": self.ctx.selected_address_id,
            }
        else:
            body = {
                "IntervalType": INTERVAL_TYPE[mission],
                "DeliveryStoreId": self.ctx.store_id or self.cfg.pickup_store_id,
            }
        if payment_type_id is not None:
            body["PaymentTypeId"] = _as_int(payment_type_id)
        payload = self.t.rest("userAddressDeliveryIntervalSearchsynced", body)
        raw = payload.get("Body", {}).get(INTERVAL_LIST_KEY[mission], [])
        return [Interval.model_validate(i) for i in raw]

    def pick_interval(self, intervals: list[Interval], index: int = 0) -> Interval:
        """Детерминированный выбор: index-й СРЕДИ ДОСТУПНЫХ.

        Старое `intervals[2]` брало третий из сырого списка — состав списка
        зависит от времени суток и загрузки слотов, отсюда флак.
        """
        available = [i for i in intervals if i.is_available]
        if not available:
            raise LoApiError("pick_interval",
                             f"нет доступных интервалов (всего в ответе: {len(intervals)})")
        return available[min(index, len(available) - 1)]


# ---------------------------------------------------------------------------
class CartClient(_Client):
    def lookup(self) -> Cart:
        payload = self.t.rest("cartLookup", {"Return": {"NeedCheckDelivery": 1, "Goods": 1}})
        return Cart.from_body(payload.get("Body", {}))

    def modify_item(self, sku: str, quantity: float) -> dict:
        return self.t.rest("cartItemModify", {
            "GoodsItemId": sku,
            "Quantity": quantity,
            "Chips": 0,
            "Return": {"Cart": 1, "Goods": 1, "ShowCartItemModifyNotices": 1},
        })

    def add(self, sku: str, quantity: float = 1) -> dict:
        return self.modify_item(sku, quantity)

    def remove(self, sku: str) -> dict:
        return self.modify_item(sku, 0)

    def set_interval(self, interval: Interval) -> dict:
        # NoProductActionID браузер здесь не шлёт — для него есть отдельный
        # эндпоинт noproductactions
        return self.t.rest("cartModify", {"DeliveryIntervalId": interval.id})

    def from_order(self, order_id: str | int) -> Cart:
        """Товары прошлого заказа — в корзину: cartFromOrderAdd.

        Так работает «Повторить заказ» в веб-морде. Весовой стенд раскладывает
        сам: 2,1 кг из заказа приходят в корзину тремя порциями по 700 г, как
        и были заказаны. Тело — как у браузера, `ForUpdate: false` значит
        «новая корзина», а не правка того же заказа.
        """
        payload = self.t.rest("cartFromOrderAdd", {
            "OrderId": str(order_id),
            "ForUpdate": False,
            "Return": {"Cart": 1, "CartItemList": 1, "TotalCost": 1, "Goods": False},
            "Source": 1,
        })
        log.info("Товары заказа %s положены в корзину", order_id)
        return Cart.from_body(payload.get("Body", {}))

    def clear(self) -> None:
        """Очистка корзины перед тестом и после него.

        Вызывается ДО теста тоже — иначе мусор от упавшего предыдущего прогона
        искажает результат.
        """
        try:
            cart = self.lookup()
        except LoApiError as exc:
            log.warning("clear: не удалось прочитать корзину (%s)", exc)
            return
        for good in cart.goods:
            try:
                self.remove(good.id)
            except LoApiError as exc:
                log.warning("clear: не удалось удалить %s (%s)", good.id, exc)


# ---------------------------------------------------------------------------
class PaymentClient(_Client):
    def search(self) -> tuple[list[PaymentType], str | None]:
        """Список способов оплаты и Id способа по умолчанию."""
        payload = self.t.rest("paymentTypeSearch", {
            "orderId": "",
            "SupportAndroidPay": "",
            "SupportApplePay": "",
            "SupportGooglePay": "",
            "SupportSamsungPay": "",
            "SupportSberPay": "",
            "SupportSbp": 1,
            "Return": {},
        })
        body = payload.get("Body", {})
        types = [PaymentType.model_validate(p) for p in body.get("PaymentTypeList", [])]
        default_raw = (body.get("DefaultPaymentType") or {}).get("typeId")
        default_id = None if default_raw is None else str(default_raw)
        return types, default_id

    @staticmethod
    def find(types: list[PaymentType], payment_id: str | None) -> PaymentType | None:
        """Поиск с приведением типов.

        Старый код сравнивал `pt["Id"] == str(typeId)`: при int в Id совпадение
        никогда не наступало и способ по умолчанию молча не находился.
        """
        if payment_id is None:
            return None
        return next((p for p in types if str(p.id) == str(payment_id)), None)


# ---------------------------------------------------------------------------
class OrderClient(_Client):
    def lookup(self, order_id: str | int) -> PastOrder:
        """Заказ покупателя по номеру — orderLookup, как в личном кабинете.

        Видны только заказы текущего аккаунта: чужой номер вернёт пустой
        список, а не ошибку.
        """
        payload = self.t.rest("orderLookup", {
            "Return": {"OrderItem": 1, "Substitutes": 1, "TripPath": 1, "UserAddress": 1},
            "Id": str(order_id),
            "Source": "account",
        })
        orders = (payload.get("Body") or {}).get("OrderList") or []
        if not orders:
            raise LoApiError(
                "orderLookup",
                f"заказ {order_id} не найден у этого аккаунта. Заказ виден только тому, "
                f"кто его оформил: войдите под его телефоном и выберите ту же организацию",
            )
        return PastOrder.model_validate(orders[0])

    def save_buyer_note(self, text: str = "") -> dict:
        """Комментарий к заказу.

        Браузер зовёт это непосредственно перед orderAdd даже с пустым текстом,
        поэтому шаг воспроизводится, а не пропускается.
        """
        return self.t.rest("orderBuyerNoteSaveCart", {"text": text})

    def add(self, payment_type_id: str | int, *, address_vsd: str | None = None,
            partner_card: str | None = None) -> str:
        """Создаёт заказ, возвращает PendingRequestKey.

        retry=False принципиально: orderAdd неидемпотентен, повтор на 502 может
        создать второй заказ.
        """
        card = partner_card or self.cfg.partner_discount_card
        body: dict[str, Any] = {
            "PaymentSuccessUrl": f"{self.cfg.base_url}/ordering/thanks/success",
            "PaymentCancelUrl": f"{self.cfg.base_url}/ordering/thanks/fail",
            # PaymentTypeId уходит числом — так шлёт браузер
            "PaymentTypeId": _as_int(payment_type_id),
            "PaymentBindingId": "",
            "PartnerDiscountCard": card,
            "ProData": {"AddressVSD": address_vsd},
        }
        try:
            payload = self.t.rest("orderAdd", body, retry=False)
        except BusinessError as exc:
            # Стенд отклоняет заказ без карты лояльности, но причину не называет:
            # приходит OrderCreationException с пустым Message и заглушкой
            # «повторите попытку» в Description. Подтверждено экспериментом:
            # два одинаковых заказа, разница только в этом поле.
            if card:
                raise
            raise BusinessError(
                "orderAdd",
                f"{exc.error}. Заказ ушёл без карты лояльности, а без неё стенд "
                f"его отклоняет. Обычно карта берётся у организации "
                f"(proCardNumber в b2b/legalEntity) — раз её там нет, задайте "
                f"явно: --partner-card или LO_PARTNER_DISCOUNT_CARD",
                payload=exc.payload, trace_id=exc.trace_id,
            ) from exc
        resp_body = payload.get("Body", {})
        key = payload.get("PendingRequestKey") or resp_body.get("PendingRequestKey")
        if not key:
            raise BusinessError("orderAdd", "не вернулся PendingRequestKey", payload=payload)
        # Номер заказа приходит сразу, ещё до подтверждения через responseLookup
        order_id = resp_body.get("OrderId")
        log.info("orderAdd принят: OrderId=%s, PendingRequestKey=%s", order_id, key)
        self.ctx.extra["last_order_id"] = order_id
        return key

    def response_lookup(self, pending_key: str) -> dict:
        return self.t.rest("responseLookup", {"PendingRequestKey": pending_key})

    def wait_for_order(self, pending_key: str, *, timeout: float | None = None,
                       poll: float | None = None) -> Order:
        """Ожидание асинхронного создания заказа.

        orderAdd возвращает PendingRequestKey — заказ создаётся асинхронно.
        Старый код звал responseLookup ровно один раз, немедленно, и при любой
        задержке бэкенда печатал «OrderList пуст» при успешно созданном заказе.
        """
        timeout = timeout if timeout is not None else self.cfg.order_wait_timeout
        poll = poll if poll is not None else self.cfg.order_poll_interval
        deadline = time.monotonic() + timeout
        last: dict | None = None
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            last = self.response_lookup(pending_key)
            orders = (
                last.get("Body", {}).get("Response", {}).get("Body", {}).get("OrderList", [])
            )
            if orders:
                order = Order.model_validate(orders[0])
                log.info("Заказ создан за %s опрос(ов): Id=%s", attempt, order.id)
                return order
            time.sleep(poll)

        raise LoApiError(
            "responseLookup",
            f"заказ не появился за {timeout:.0f}с ({attempt} опросов). "
            f"Последний ответ: {str(last)[:800]}",
        )


# ---------------------------------------------------------------------------
class LentaApi:
    """Фасад: один транспорт, один контекст, все клиенты."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.t = Transport(ctx)
        self.auth = AuthClient(self.t)
        self.b2b = B2bClient(self.t)
        self.user = UserClient(self.t)
        self.address = AddressClient(self.t)
        self.store = StoreClient(self.t)
        self.delivery = DeliveryClient(self.t)
        self.cart = CartClient(self.t)
        self.payment = PaymentClient(self.t)
        self.order = OrderClient(self.t)

    def close(self) -> None:
        self.t.close()


def _as_int(value: str | int) -> int | str:
    """Приводит Id к числу, если это возможно.

    Стенд отдаёт PaymentTypeId строкой ("702") в списке способов оплаты, но
    принимает числом в orderAdd и в запросе интервалов.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value
