"""Сборка заказа сборщиком и доставка курьером.

Порт SIMPLE-ветки коллекции B2B-Pick (папка Pick-Deliv). Полная коллекция
маршрутизирует шесть типов позиций; здесь реализован штучный немаркированный
товар — остальные требуют генерации марок Datamatrix, штрихкодов весового
товара и работы с партиями SAP, и распознаются, чтобы отказать внятно, а не
собрать заказ наполовину.

Порядок подтверждён коллекцией:
    сборщик: orderGet → orderPaymentCheck → ASSEMBLE_INCOMPLETE →
             (itemUpdateByBarcode [→ orderUpdate count]) × позиции →
             getSkuGroupsOrderBySection → PACKAGING → getPackages →
             ASSEMBLE_COMPLETE
    курьер:  orderDeliveryTaskList → orderGet → DELIVERING → WAITING_CLIENT →
             DELIVERED
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date

from lo_api.goods import GoodsClient, GoodsError
from lo_api.marking import (
    generate_marking,
    kind_from_goods,
    resolve_marking_kind,
    weight_barcode,
    weight_barcode_issues,
)
from lo_api.picker import PickerError, PickerRpc

log = logging.getLogger("lo_api.picking")

# Типы позиций из classifyItem коллекции. Собираются все.
SUPPORTED_KINDS = frozenset({
    "SIMPLE", "BATCH", "MARK_PEU", "MARK_OSU", "MARK_PEU_BATCH", "MARK_OSU_BATCH",
    "SAMPLE", "SAMPLE_SKU",
})

KIND_REASON: dict[str, str] = {}


# Куда вставать при заданном статусе заказа — таблица RESUME из flow.js.
RESUME = {
    "ACCEPTED": "assemble",
    "ASSEMBLE_INCOMPLETE": "assemble",
    "SAP_VALIDATING_STARTED": "poll",
    "SAP_VALIDATING": "poll",
    "SAP_VALIDATING_SUCCESS": "packaging",
    # По эталону (10.3) ошибка проверки — не конец: проблемное убирается и
    # пересобирается, заказ уходит на проверку повторно
    "SAP_VALIDATING_ERROR": "fix",
    "PACKAGING": "packages",
    "ASSEMBLE_COMPLETE": "delivery",
    "DELIVERING": "waiting",
    "WAITING_CLIENT": "delivered",
    "DELIVERED": "done",
    "FINISHED": "done",
}

RESUME_WHY = {
    "assemble": "сборка с начала (уже собранное дособерём)",
    "poll": "валидация в SAP идёт — ждём её, пересобирать не нужно",
    "packaging": "валидация SAP пройдена — сборку пропускаем, идём к упаковке",
    "packages": "заказ в упаковке — продолжаем с getPackages",
    "delivery": "заказ скомплектован — сразу к шагам курьера",
    "waiting": "заказ в пути — продолжаем с WAITING_CLIENT",
    "delivered": "курьер у клиента — продолжаем с DELIVERED",
    "done": "заказ уже доставлен — прогонять нечего",
    "stop": "валидация в SAP завершилась ошибкой — дальше идти нельзя",
    "fix": "валидация SAP вернула ошибку — пересоберу проблемное и отправлю снова",
}

# Статусы курьера по порядку: возобновление берёт хвост списка.
DELIVERY_STATUSES = ("DELIVERING", "WAITING_CLIENT", "DELIVERED")

# Заказ доставлен. B2B с оплатой по расчётному счёту уезжает из DELIVERED
# в FINISHED сразу — довзыскивать нечего.
FINAL_STATUSES = frozenset({"DELIVERED", "FINISHED"})

# Заказ самовывоза курьеру не отдаётся — флоу кончается на ASSEMBLE_COMPLETE.
PICKUP_PREFIX = "462"

# Ответы sapValidate, означающие «валидация идёт», а не отказ: приходят
# с HTTP 200, но в поле error.
SAP_IN_PROGRESS_CODES = frozenset({4333, 3358})


def status_of(order: dict) -> str:
    return str((order.get("statusInfo") or {}).get("alias") or "")


#: «Невозможно начать сборку нескольких заказов. Открытый заказ 900001126237»
OPEN_ORDER_RE = re.compile(r"нескольких заказов.*?(\d{6,})", re.S | re.I)

#: «Нельзя начать доставку маршрута при наличии несобранных заказов (orderId: …)»
ROUTE_BLOCKED_RE = re.compile(r"несобранных заказов.*?(\d{6,})", re.S | re.I)

#: Причина отмены, если список причин со стенда не разобрался: так в запросе
#: orderPickerCancel коллекции PickerApp
FALLBACK_CANCEL_REASON = 1


class OpenOrderError(PickerError):
    """Сборщик уже собирает другой заказ, и начать новый стенд не даёт.

    Держать сразу два заказа в сборке сборщику нельзя. Что делать с открытым —
    дособрать или отменить — решает человек, поэтому библиотека только
    поднимает ошибку с номером мешающего заказа.
    """

    open_order_id: str = ""


class RouteBlockedError(PickerError):
    """Маршрут курьера не стартует: в нём есть несобранный заказ.

    TMS складывает заказы курьера в маршрут, и в доставку он уходит только
    целиком. Недособранный заказ из прошлых прогонов держит весь маршрут —
    включая уже собранный заказ, который мы везём.
    """

    blocking_order_id: str = ""


def find_cancel_reasons(node: object, depth: int = 0) -> list[dict]:
    """Причины отмены из ответа orderReasonsCancel: `[{id, title}]`.

    Форма ответа по коллекции неизвестна, поэтому ищется список объектов с
    полем id и каким-нибудь текстом — на любой глубине.
    """
    if depth > 5:
        return []
    if isinstance(node, list):
        found = []
        for item in node:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            title = next((str(item[k]) for k in ("name", "title", "text", "reason",
                                                   "description", "value") if item.get(k)), "")
            if title:
                found.append({"id": item["id"], "title": title})
        if found:
            return found
        node = [x for x in node if isinstance(x, (dict, list))]
        children = node
    elif isinstance(node, dict):
        children = list(node.values())
    else:
        return []
    for child in children:
        found = find_cancel_reasons(child, depth + 1)
        if found:
            return found
    return []


def resume_point(order: dict) -> tuple[str, str]:
    """С какого шага продолжать заказ и почему."""
    alias = status_of(order).upper()
    action = RESUME.get(alias)
    if action is None:
        return "assemble", f"статус {alias or '—'} незнакомый, идём с начала сборки"
    return action, RESUME_WHY[action]


def is_pickup_id(order_id: object) -> bool:
    """Самовывоз по одному номеру заказа, без самого заказа.

    Нужно там, где заказ ещё не читали: назначение в TMS идёт до первого
    orderGet, а курьер самовывозу не полагается.
    """
    digits = "".join(c for c in str(order_id or "") if c.isdigit())
    return digits.startswith(PICKUP_PREFIX)


def is_pickup_order(order: dict) -> bool:
    """Самовывоз узнаётся по номеру заказа или по названию интервала."""
    ids = [order.get("id"), order.get("hubOrderId"),
           (order.get("printableInfo") or {}).get("orderNumber")]
    for value in ids:
        if is_pickup_id(value):
            return True
    delivery = order.get("deliveryInfo") or {}
    name = str(delivery.get("deliveryIntervalName") or "").lower()
    kind = str(delivery.get("deliveryIntervalType") or "").lower()
    return "самовывоз" in name or "pickup" in kind


# Заглушка в поле исполнителя: имя «Dummy» либо buyerId = INT_MAX с пустым
# именем. И то и другое означает «настоящего исполнителя нет».
DUMMY_PERFORMER_MARKER = "dummy"
UNASSIGNED_BUYER_ID = 2147483647


def performer_of(order: dict, key: str) -> dict | None:
    """Настоящий исполнитель из pickerStaffInfo / courierStaffInfo, иначе None.

    Заказ без назначения отдаёт по этим ключам `null`, заказ на заглушке —
    объект с именем «Dummy». Ни то, ни другое сборку не запустит.
    """
    info = order.get(key)
    if not isinstance(info, dict):
        return None
    name = str(info.get("name") or "").strip()
    if not name or DUMMY_PERFORMER_MARKER in name.lower():
        return None
    if info.get("buyerId") in (None, "", UNASSIGNED_BUYER_ID):
        return None
    return info


def assignment_issues(order: dict) -> list[str]:
    """Кого не хватает, чтобы заказ можно было собирать. Пусто — назначен.

    Самовывоз курьеру не отдаётся, поэтому его отсутствие тут не проблема:
    иначе такой заказ переназначался бы перед каждой сборкой впустую.
    """
    roles = [("сборщик", "pickerStaffInfo")]
    if not is_pickup_order(order):
        roles.append(("курьер", "courierStaffInfo"))
    return [role for role, key in roles if performer_of(order, key) is None]


def assigned_performers(order: dict) -> dict[str, str]:
    """Кто назначен сейчас: роль -> имя. Заглушки и пустые места пропускаются."""
    found = {}
    for role, key in (("сборщик", "pickerStaffInfo"), ("курьер", "courierStaffInfo")):
        info = performer_of(order, key)
        if info:
            found[role] = str(info.get("name"))
    return found


OWN_NEEDS_KEYS = ("markOwnNeeds", "vetOwnNeeds")

#: Признак ВСД называется в ответах по-разному: vetCertificateRequired (так в
#: эталоне), hasVetCertificate (orderUpdate, сканы) и vetCertificate строкой
#: «Нужны» / «Не нужны» (orderGet). Документ считает их одним признаком.
VSD_KEYS = ("vetCertificateRequired", "vetCertificate", "hasVetCertificate")


def find_pro_info(node: object, depth: int = 0,
                  keys: tuple[str, ...] = OWN_NEEDS_KEYS) -> dict | None:
    """Ищет proInfo с нужными флагами в ответе любой вложенности.

    Порт findProInfo: флаги приходят не в orderGet, а в ответах orderUpdate и
    сканов, причём на разной глубине.
    """
    if not isinstance(node, (dict, list)) or depth > 8:
        return None
    if isinstance(node, list):
        for child in node:
            found = find_pro_info(child, depth + 1, keys)
            if found:
                return found
        return None

    pro = node.get("proInfo")
    if isinstance(pro, dict) and any(k in pro for k in keys):
        return pro
    for value in node.values():
        found = find_pro_info(value, depth + 1, keys)
        if found:
            return found
    return None


def vsd_required_from(payload: object) -> bool | None:
    """Нужен ли заказу ВСД. None — в ответе признака нет."""
    pro = find_pro_info(payload, keys=VSD_KEYS)
    if pro is None:
        return None
    if isinstance(pro.get("vetCertificateRequired"), bool):
        return pro["vetCertificateRequired"]
    text = str(pro.get("vetCertificate") or "").strip().lower()
    if text in ("нужны", "нужен", "да"):
        return True
    if text.startswith("не"):
        return False
    if isinstance(pro.get("hasVetCertificate"), bool):
        return pro["hasVetCertificate"]
    return None


def own_needs_from(payload: object) -> bool | None:
    """Собственные нужды по ответу стенда: нужны ОБА флага.

    None — в ответе флагов нет, прежнее значение остаётся в силе.
    """
    pro = find_pro_info(payload)
    if pro is None:
        return None
    return pro.get("markOwnNeeds") is True and pro.get("vetOwnNeeds") is True


def is_weight(item: dict) -> bool:
    """Весовой товар. Не тип, а модификатор: меняет штрихкод и смысл count.

    У весового `count` — граммы, а число единиц лежит в `portion`.
    """
    return item.get("unit") == "GW"


#: Типы контроля партионного учёта: A — алкоголь (ЕГАИС), V — вет (Меркурий).
#: Раньше проверялся только V, и пиво (A) собиралось как ПЭУ без партий.
BATCH_CONTROL_TYPES = frozenset({"A", "V"})


def classify(item: dict) -> str:
    """Тип позиции — в порядке приоритета признаков из эталонного документа.

    1. `isMarking` — ПЭУ, марка с каждой единицы;
    2. `isBatchMarking` — ПЭУ с партиями (b2b, controlType A/V и zInd);
    3. `controlType` A или V — партионный учёт;
    4. `isOsuMarkRequired` — ОСУ, одна марка на позицию.

    `isMarking` ставит PriceEngine уже на сборке, а до неё в orderGet приходит
    null. Тогда о маркировке говорит `isMarkingInGoods`, и ОСУ определяется
    по своему признаку, как раньше.
    """
    if item.get("sampleId") is not None:
        return "SAMPLE_SKU" if item.get("sku") else "SAMPLE"

    if item.get("isBatchMarkingAssembly") is True:
        return "MARK_OSU_BATCH"

    batch = item.get("controlType") in BATCH_CONTROL_TYPES
    batch_marking = item.get("isBatchMarking") is True
    # Партии у ПЭУ: явный признак стенда либо тип контроля. Только явный
    # признак брать нельзя — у икры он бывает false при controlType V
    peu_batch = batch_marking or batch

    if item.get("isMarking") is True or batch_marking:
        return "MARK_PEU_BATCH" if peu_batch else "MARK_PEU"

    marked = item.get("isMarkingInGoods") is True
    if item.get("isOsuMarkRequired") is True and (
        marked or item.get("isBatchMarkingAssembly") is False
    ):
        return "MARK_OSU_BATCH" if batch else "MARK_OSU"
    if marked:
        return "MARK_PEU_BATCH" if peu_batch else "MARK_PEU"
    if batch:
        return "BATCH"
    return "SIMPLE"


def normalize_sku(sku: object) -> str:
    """В заказе sku приходит с ведущими нулями, а методы ждут его без них."""
    text = str(sku or "").strip()
    return text.lstrip("0") or "0" if text.isdigit() else text


@dataclass
class PickTask:
    """Одна позиция к сборке на конкретном складе."""

    sku: str
    barcode: str
    count: int
    stock_id: int | None
    store_id: int | None
    kind: str = "SIMPLE"
    name: str = ""
    # Число марок: у ОСУ одна на позицию, у ПЭУ — по одной на единицу
    marks: int = 0
    # Партии из заказа: их количество задаёт число вызовов sapBatchAdd
    batches: list = field(default_factory=list)
    # Весовой: count тогда в граммах, а единиц — units
    weight: bool = False
    units: int = 0
    sample_id: object = None
    # sku как в заказе, с ведущими нулями. Большинство методов ждут его без
    # нулей (sku), а sapMarking — именно таким: стенд сверяет его строкой
    # с SKU, найденным по GTIN марки
    order_sku: str = ""
    # Причины, по которым позицию можно убрать из заказа: {"3": "Не найден в ТЗ"}.
    # Приходят прямо в позиции orderGet
    missing_reasons: dict = field(default_factory=dict)

    batch_marking_assembly: bool | None = None
    mark_type: str = ""
    ordered_count: int = 0
    shortage_reason: str | None = None

    @property
    def position_key(self) -> tuple:
        return self.sku, self.sample_id, self.stock_id

    def select_count(self, count: int, units: int) -> None:
        """count — сколько добавить сейчас, ordered_count — итог по складу."""
        self.ordered_count -= self.count - count
        self.count, self.units = count, units
        if self.kind.startswith("MARK_PEU"):
            self.marks = units

    @property
    def batch_count(self) -> int:
        return len(self.batches) or 1

    def __str__(self) -> str:
        return f"{self.name or self.sku} ×{self.count} ({self.kind})"


@dataclass
class PickingPlan:
    tasks: list[PickTask] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    picked: list[str] = field(default_factory=list)  # что уже собрано до нас


def build_plan(order: dict, *, resume: bool = False,
               targets: dict[tuple, PickTask] | None = None) -> PickingPlan:
    """Очередь сборки из ответа orderGet.

    Позиция может быть разложена по нескольким складам — тогда на каждый
    склад свой шаг со своими stockId/storeId, как в buildQueue коллекции.

    resume=True для заказа в ASSEMBLE_INCOMPLETE: берётся только недостающее
    количество, а полностью собранные позиции пропускаются. Без этого повтор
    прогона пересобирает уже собранное — SAP отвечает «только одна несобранная
    партия может быть без ИНН», потому что партию заводят второй раз.
    """
    plan = PickingPlan()
    warehouse = (order.get("warehouses") or [{}])[0]
    default_stock = warehouse.get("id", 1)
    default_store = warehouse.get("storeId")

    def add(item: dict) -> None:
        # Убранная из заказа позиция (missingReasonId) не собирается: её
        # countPick = 0 — это решение, а не недосбор
        if item.get("missingReasonId") is not None:
            plan.picked.append(f"{item.get('sku')} ({item.get('name', '')}): убран из "
                               f"заказа — {removal_text(item)}")
            return
        kind = classify(item)
        if kind not in SUPPORTED_KINDS:
            reason = KIND_REASON.get(kind, kind)
            plan.unsupported.append(f"{item.get('sku')} ({item.get('name', '')}): {reason}")
            return

        records = (item.get("stockData") or {}).get("records") or []
        places = records or [{
            "count": item.get("count") if item.get("count") is not None
                     else item.get("countPick"),
            # У замены count пуст, и фактическое количество лежит в countPick —
            # тогда собранным считать нечего
            "countPick": item.get("countPick") if item.get("count") is not None else 0,
            "id": default_stock, "storeId": default_store,
        }]
        weight = is_weight(item)
        for place in places:
            ordered = int(place.get("count") or 0)
            key = normalize_sku(item.get("sku")), item.get("sampleId"), place.get("id")
            target = (targets or {}).get(key)
            if target:
                ordered = target.ordered_count
            picked = int(place.get("countPick") or 0)
            if resume and picked:
                # Весовой добирать частично нельзя: либо он собран, либо
                # взвешиваем заново целиком
                left = 0 if weight and picked >= ordered * 0.9 else (
                    ordered if weight else max(ordered - picked, 0))
                plan.picked.append(
                    f"{item.get('sku')} ({item.get('name', '')}): собрано {picked} "
                    f"из {ordered}" + (f", осталось {left}" if left else "")
                )
            else:
                left = ordered
            if left <= 0:
                continue

            # У весового count — граммы, а единиц столько, сколько порций
            units = int(item.get("portion") or 1) if weight else left
            if weight and target:
                units = target.units
            # ОСУ — одна марка на любое количество, ПЭУ — по одной на единицу.
            # Считаем от остатка, а не от заказанного
            marks = 0
            if kind.startswith("MARK"):
                marks = 1 if item.get("isOsuMarkRequired") is True else units
            plan.tasks.append(PickTask(
                sku=normalize_sku(item.get("sku")),
                order_sku=str(item.get("sku") or ""),
                missing_reasons=dict(item.get("missingReasons") or {}),
                barcode=str(item.get("barcode") or ""),
                count=left,
                ordered_count=ordered,
                batch_marking_assembly=item.get("isBatchMarkingAssembly"),
                mark_type=str(item.get("markType") or ""),
                stock_id=place.get("id"),
                store_id=place.get("storeId"),
                kind=kind,
                name=str(item.get("name") or ""),
                marks=marks,
                batches=list(item.get("batches") or []),
                weight=weight,
                units=units,
                sample_id=item.get("sampleId"),
            ))

    for item in order.get("itemsList") or []:
        add(item)
        # Замена — товар со своими признаками, может быть маркированной
        if item.get("replacement"):
            add(item["replacement"])
    return plan


def removal_text(item: dict) -> str:
    """Почему позиция убрана: текст причины, если стенд его отдал."""
    reason = item.get("missingReasonId")
    text = item.get("missingReasonText") or (item.get("missingReasons") or {}).get(str(reason))
    return f"{str(text).strip()} ({reason})" if text else f"причина {reason}"


@dataclass
class BatchSpec:
    """Одна партия товара: производитель, дата производства, сколько в ней."""

    manufacturer: dict
    date_iso: str
    count: int

    def __str__(self) -> str:
        name = self.manufacturer.get("name") or self.manufacturer.get("code")
        return f"{name}, {self.date_iso}, {self.count}"


class PickDecisions:
    """Что решает человек по ходу сборки. Базовый класс — автоматическая сборка.

    Библиотека не задаёт вопросов сама (в ней нет input), а спрашивает этот
    объект. CLI подставляет свой — с вопросами по каждому товару.
    """

    def remove_reason(self, task: PickTask) -> str | None:
        """Убрать позицию из заказа? None — собирать, иначе id причины."""
        return None

    def dairy_mark(self, task: PickTask, barcode: str) -> str:
        return generate_marking(barcode, "milk")

    def retry_batch_marking_info(self, error: PickerError) -> bool:
        return False

    def dairy_batch(self, manufacturers: list[dict], date_iso: str) -> tuple[dict, str]:
        return manufacturers[0], date_iso

    def batches(self, client: PickingClient, order_id: str,
                task: PickTask) -> list[BatchSpec] | None:
        """Партии позиции. None — подобрать автоматически, по настройкам стенда."""
        return None


class PickingClient(PickerRpc):
    """Методы сборки и доставки. Порядок вызовов — в функциях ниже."""

    #: Заказ в собственные нужды. Читается из ответов стенда по ходу сборки.
    own_needs: bool = False
    #: Нужен ли заказу ВСД. None — стенд пока не сказал.
    vsd_required: bool | None = None
    #: Кто решает по ходу сборки: автоматически или человек
    decisions: PickDecisions = PickDecisions()
    #: Следить ли за признаками СН и ВСД. Они решают только, запрашивать ли
    #: партии, поэтому включаются на время pick_order — и ни при проверке
    #: назначения, ни у курьера в журнал не лезут
    tracks_assembly_flags: bool = False

    def __init__(self, cfg):
        super().__init__(cfg)
        self.partial_targets: dict[tuple, PickTask] = {}

    def remember_own_needs(self, payload: object) -> None:
        """Обновляет признаки собственных нужд и ВСД из ответа, если они там есть."""
        if not self.tracks_assembly_flags:
            return
        vsd = vsd_required_from(payload)
        if vsd is not None and vsd != self.vsd_required:
            self.vsd_required = vsd
            log.info("ВСД по заказу %s", "нужен" if vsd else "не нужен")
        detected = own_needs_from(payload)
        if detected is None or detected == self.own_needs:
            return
        self.own_needs = detected
        # Без обобщений: партии пропускаются не у всех партионных — у
        # алкоголя они нужны всегда. Что именно с позицией, пишет _pick_task
        log.info("Заказ %s собственные нужды (markOwnNeeds и vetOwnNeeds %s)",
                 "в" if detected else "не в", "стоят" if detected else "не стоят разом")

    def is_own_needs(self) -> bool:
        """Ручная настройка важнее того, что сказал стенд."""
        if self.cfg.own_needs is not None:
            return self.cfg.own_needs
        return self.own_needs

    def call(self, method: str, params: dict | None = None, **kw) -> dict:
        """Как у PickerRpc, но «открыт другой заказ» узнаётся отдельно.

        Стенд отвечает на это HTTP 200 с ошибкой в теле, и в тексте лежит
        номер мешающего заказа — его и надо показать человеку.
        """
        try:
            return super().call(method, params, **kw)
        except (OpenOrderError, RouteBlockedError):
            raise
        except PickerError as exc:
            text = exc.reason or str(exc)
            if match := OPEN_ORDER_RE.search(text):
                blocked = OpenOrderError(exc.method, text, status=exc.status,
                                         payload=exc.payload)
                blocked.open_order_id = match.group(1)
            elif match := ROUTE_BLOCKED_RE.search(text):
                blocked = RouteBlockedError(exc.method, text, status=exc.status,
                                            payload=exc.payload)
                blocked.blocking_order_id = match.group(1)
            else:
                raise
            blocked.reason = exc.reason
            raise blocked from exc

    def goods_card(self, task: PickTask) -> dict | None:
        """Карточка товара из Goods — один запрос на SKU за прогон.

        Отсюда берутся группа марки (`markType`) и признак алкоголя
        (`isAlcohol`): в orderGet их нет. Goods недоступен — не повод ронять
        сборку: тогда работают настройки стенда, о чём говорится в журнале.
        """
        cards = self.__dict__.setdefault("_goods_cards", {})
        if task.sku not in cards:
            store = task.store_id or self.cfg.goods_store_id
            goods = GoodsClient(self.cfg)
            try:
                cards[task.sku] = goods.card(store, task.sku)
            except GoodsError as exc:
                log.warning("Goods не отдал карточку %s (%s) — группа марки и признак "
                            "алкоголя берутся из настроек стенда", task.sku, exc)
                cards[task.sku] = None
            finally:
                goods.close()
        return cards[task.sku]

    def remove_item(self, order_id: str, task: PickTask, reason_id: str) -> dict:
        """Убрать позицию из заказа с причиной — как «удалить товар» в приложении.

        Тело — из коллекции PickerApp (orderUpdate MissingReason). На стенде
        так убранная позиция приходит с missingReasonId и countPick = 0.
        """
        result = self.set_missing_reason(order_id, task.sku, reason_id)
        log.info("Убран из заказа: %s (%s) — причина %s", task.sku, task.name,
                 task.missing_reasons.get(str(reason_id), reason_id))
        return result

    def set_missing_reason(self, order_id: str, sku: str, reason_id: str) -> dict:
        return self.call("orderUpdate", {"order": {"id": str(order_id), "items": [{
            "sku": sku, "missingReasonId": str(reason_id), "missingReasonComment": "",
        }]}})

    def cancel_reasons(self) -> list[dict]:
        """Причины отмены для приложения сборщика — `[{id, title}]`."""
        return find_cancel_reasons(self.call("orderReasonsCancel", {}))

    def cancel_order(self, order_id: str, reason_id: object) -> dict:
        """Отмена заказа сборщиком с причиной — orderPickerCancel.

        Из двух ручек коллекции PickerApp выбрана эта, а не orderUpdate со
        статусом CANCELED: она описана как отмена пикером, с причиной.
        """
        result = self.call("orderPickerCancel",
                           {"orderId": str(order_id), "reasonId": reason_id})
        log.info("Заказ %s отменён сборщиком (причина %s)", order_id, reason_id)
        return result

    def order_get(self, order_id: str) -> dict:
        """Заказ лежит в `result.order`, а не в `result` напрямую.

        Возврат `result` целиком давал пустой `itemsList` и невнятное
        «нечего собирать» при непустом заказе.
        """
        result = self.call("orderGet", {"orderId": str(order_id)})
        # В orderGet признак ВСД есть (vetCertificate), а флагов СН нет
        self.remember_own_needs(result)
        order = result.get("order")
        if order is None:
            raise PickerError("orderGet",
                              f"в ответе нет result.order, только {sorted(result)}. "
                              f"Похоже, контракт метода изменился")
        return order

    def payment_check(self, order_id: str) -> dict:
        return self.call("orderPaymentCheck", {"orderId": str(order_id)})

    def set_status(self, order_id: str, status: str) -> dict:
        result = self.call("orderUpdate",
                           {"order": {"id": str(order_id), "status": status}})
        self.remember_own_needs(result)
        return result

    def scan(self, task: PickTask, order_id: str) -> dict:
        """Скан штрихкода. Кладёт в заказ ровно одну единицу."""
        result = self.call("itemUpdateByBarcode", {
            "orderId": str(order_id),
            "barcode": task.barcode,
            "stockId": task.stock_id,
            "storeId": task.store_id,
        })
        # Флаги собственных нужд приходят в ответе скана — читаем до проверок
        self.remember_own_needs(result)
        # Метод отвечает 200 и при этом может не положить товар: причина
        # приезжает в errorOrderList, пересорт — отдельным флагом
        updated = ((result.get("data") or {}).get("updated")) or {}
        errors = updated.get("errorOrderList") or []
        if errors:
            raise PickerError("itemUpdateByBarcode",
                              f"скан {task.barcode} отклонён: {errors}", payload=result)
        if updated.get("isRegradingItemError") is True:
            raise PickerError("itemUpdateByBarcode",
                              f"пересорт по позиции {task.sku}", payload=result)
        return result

    def set_count(self, order_id: str, task: PickTask) -> dict:
        """Проставляет количество: один скан кладёт одну единицу.

        `stock_count` задаёт итог, а не прибавку, и меряется в тех же единицах,
        что `count` позиции: у весового это граммы, а не порции. Коллекция шлёт
        сюда `unitCount()`, то есть `portion`, — и на весовом это ошибка: на
        заказе 2200 г из 4 порций стенд записывал countPick = 4, что в карточке
        показывается как 0.004 кг.
        """
        return self.call("orderUpdate", {"order": {
            "id": str(order_id),
            "items": [{"sku": task.sku, "stock_id": task.stock_id,
                       "stock_count": task.ordered_count or task.count}],
        }})

    def set_sample(self, order_id: str, task: PickTask) -> dict:
        """Проба. Со своим sku в теле идёт и он, без sku — только sampleId."""
        item: dict[str, object] = {"count": task.ordered_count or task.units}
        if task.kind == "SAMPLE_SKU":
            item["sku"] = task.sku
            item["sampleId"] = task.sample_id
        else:
            item["sampleId"] = str(task.sample_id)
        return self.call("orderUpdate",
                         {"order": {"id": str(order_id), "items": [item]}})

    # -- маркированный товар ----------------------------------------------
    def sap_marking(self, order_id: str, sku: str, marking: str) -> dict:
        return self.call("sapMarking",
                         {"sku": sku, "orderId": str(order_id), "marking": marking})

    def item_increase_by_marking(self, order_id: str, marking: str,
                                 stock_id: int | None) -> dict:
        return self.call("itemIncreaseByMarking", {
            "orderId": str(order_id), "marking": marking,
            "stockId": stock_id, "forceRegrading": False,
        })

    def item_batch_increase_by_marking(self, order_id: str, marking: str) -> dict:
        # stockId здесь всегда null — так шлёт коллекция
        return self.call("itemBatchIncreaseByMarking",
                         {"orderId": str(order_id), "marking": marking, "stockId": None})

    # -- партии SAP --------------------------------------------------------
    def sap_manufacturer(self, order_id: str, sku: str) -> list[dict]:
        result = self.call("sapManufacturer", {"sku": sku, "orderId": str(order_id)})
        if isinstance(result, list):
            return result
        for key in ("manufacturers", "list", "data", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return value
        return []

    def batch_marking_info(self, order_id: str, mark: str) -> dict:
        params = {"orderId": str(order_id), "marking": mark}
        while True:
            try:
                result = self.call("getBatchMarkingInfo", params)
                break
            except PickerError as exc:
                if not self.decisions.retry_batch_marking_info(exc):
                    raise
        info = result.get("markingInfo")
        if not isinstance(info, dict) or not isinstance(info.get("gtin"), str) or not info["gtin"]:
            raise PickerError("getBatchMarkingInfo", "в ответе отсутствует markingInfo.gtin")
        return info

    def osu_batch_add(self, order_id: str, task: PickTask, manufacturer: dict,
                      production_date: date, info: dict) -> dict:
        params = {"orderId": str(order_id), "sku": task.sku,
                  "manufacturerCode": manufacturer["code"],
                  "dateOfManufacture": production_date.strftime("%d.%m.%Y"), "gtin": info["gtin"]}
        if info.get("partNumber"):
            params["markingPartNumber"] = info["partNumber"]
        return self.call("sapOsuMarkingBatchAdd", params, attempts=1)

    def sap_batch_add(self, order_id: str, sku: str, *, code: str,
                      date_dmy: str, inn: str) -> dict:
        return self.call("sapBatchAdd", {
            "sku": sku, "orderId": str(order_id), "manufacturerCode": code,
            "dateOfManufacture": date_dmy, "manufacturerInn": inn,
        })

    def sap_batch(self, order_id: str, sku: str, *, code: str,
                  date_iso: str, count: int) -> dict:
        return self.sap_batches(order_id, sku, [
            BatchSpec(manufacturer={"code": code}, date_iso=date_iso, count=count)])

    def sap_batches(self, order_id: str, sku: str, specs: list[BatchSpec]) -> dict:
        """Партии позиции одним вызовом: sapBatch принимает список."""
        return self.call("sapBatch", {
            "sku": sku, "orderId": str(order_id),
            "batches": [{
                "manufacturerCode": str(spec.manufacturer.get("code") or ""),
                "dateOfManufacture": spec.date_iso,
                "count": spec.count, "availabilityCount": 0,
                "availabilityCountRemain": 0, "batchPosition": position,
                "isNeedReassemble": False,
            } for position, spec in enumerate(specs)],
        })

    def sap_validate(self, order_id: str) -> dict:
        """Запускает валидацию. Она асинхронная, результат читается из orderGet.

        Коды 4333 «принят в обработку» и 3358 «статус изменён» приходят с
        HTTP 200 в поле error, но означают «валидация идёт», а не отказ.
        """
        try:
            return self.call("sapValidate", {"orderId": str(order_id)})
        except PickerError as exc:
            code = ((exc.payload or {}).get("error") or {}).get("code")
            if code not in SAP_IN_PROGRESS_CODES:
                raise
            log.info("sapValidate: валидация уже идёт (код %s)", code)
            return {}

    def sku_groups(self, order_id: str) -> dict:
        return self.call("getSkuGroupsOrderBySection", {"order_id": str(order_id)})

    def packages(self, order_id: str) -> dict:
        """Типы упаковки. На стенде сервис почти всегда лежит, и это норма —
        поэтому одна попытка без повторов: ждать от них нечего."""
        return self.call("getPackages", {"order_id": str(order_id)}, attempts=1)

    def delivery_tasks(self) -> dict:
        return self.call("orderDeliveryTaskList", {})


def picked_issues(order: dict, tolerance: float = 0.1, *,
                  targets: dict[tuple, PickTask] | None = None) -> list[str]:
    """Позиции, которые собраны не полностью.

    Порт pickedIssues из flow.js — главная проверка прогона: методы могут не
    ругнуться, а товар в заказ не попасть. У весового `countPick` приходит
    фактическим весом в тех же единицах, что `count`, — с допуском на довесок.
    Числом порций он НЕ приходит: такое послабление здесь однажды пропустило
    заказ, где вместо 2200 г было собрано 4 г, ровно по числу порций.
    """
    issues = []
    pending = dict(targets or {})
    items = order.get("itemsList") or []
    if targets:
        items = [*items, *(i["replacement"] for i in items if i.get("replacement"))]
    for item in items:
        sku = normalize_sku(item.get("sku"))
        selected = {k: v for k, v in pending.items() if k[:2] == (sku, item.get("sampleId"))}
        if selected:
            records = (item.get("stockData") or {}).get("records") or []
            if records:
                for record in records:
                    key = sku, item.get("sampleId"), record.get("id")
                    target = pending.pop(key, None)
                    want = target.ordered_count if target else int(record.get("count") or 0)
                    got = int(record.get("countPick") or 0)
                    if got != want:
                        issues.append(f"{sku} (склад {key[2]}): {got}/{want}")
            elif len(selected) == 1:
                key, target = next(iter(selected.items()))
                pending.pop(key)
                got = int(item.get("countPick") or 0)
                if got != target.ordered_count:
                    issues.append(f"{sku}: {got}/{target.ordered_count}")
            continue
        if item.get("sampleId") is not None or item.get("missingReasonId") is not None:
            continue
        want = float(item.get("count") or 0)
        if not want:
            continue
        got = float(item.get("countPick") or 0)

        if item.get("unit") == "GW":
            ok = got >= want * (1 - tolerance)
            unit = "г"
        else:
            ok = got >= want
            unit = "шт"
        if not ok:
            issues.append(f"{item.get('sku')}: {got:g}/{want:g} {unit}")
    issues.extend(f"{sku}: нет результата по складу {stock}" for sku, _, stock in pending)
    return issues


def _date_for_sku(dates: dict[str, str], sku: str) -> str | None:
    """Дата по sku с учётом ведущих нулей: в заказе «064536», в конфиге «64536»."""
    if sku in dates:
        return dates[sku]
    return next((v for k, v in dates.items() if normalize_sku(k) == sku), None)


def _batch_date(cfg, task: PickTask) -> str:
    """Дата партии в ISO. Приоритет сверху вниз, как в batchDate из flow.js.

    Ручное значение важнее пришедшего в заказе: иначе переменную не выставить,
    её всё равно перебьёт batches[]. У веток sapBatchAdd и sapBatch свои
    переменные — партия, которую принимает одна, другой может не подойти.
    """
    is_add_branch = task.kind == "MARK_PEU_BATCH"
    branch_var = "LO_BATCH_DATE_ADD" if is_add_branch else "LO_BATCH_DATE_SAP"
    branch_value = cfg.batch_date_add if is_add_branch else cfg.batch_date_sap
    from_order = (task.batches[0] or {}).get("dateOfManufacture") if task.batches else None

    chain = [
        (f"LO_BATCH_DATES[{task.sku}]", _date_for_sku(cfg.batch_dates, task.sku)),
        (branch_var, branch_value),
        ("заказ, batches[]", from_order),
        ("LO_BATCH_DATE", cfg.batch_date),
    ]
    for source, value in chain:
        if value:
            log.info("Дата партии %s: %s (%s)", task.sku, str(value)[:10], source)
            return str(value)[:10]

    raise PickerError(
        "sapBatch",
        f"не задана дата партии для sku {task.sku} ({task.kind}). Заполните одну "
        f"из переменных, сверху вниз по приоритету:\n"
        f'    LO_BATCH_DATES={{"{task.sku}": "ГГГГ-ММ-ДД"}}  — только этому товару\n'
        f"    {branch_var}=ГГГГ-ММ-ДД                — всей этой ветке\n"
        f"    LO_BATCH_DATE=ГГГГ-ММ-ДД                 — всем партионным\n"
        f"Подставлять выдуманную дату нельзя: SAP ответит «партия с указанными "
        f"признаками не найдена», и это выглядит случайным сбоем",
    )


def default_batch_date(cfg, task: PickTask) -> str | None:
    """Дата партии из настроек или заказа — подсказка для вопроса. None — нет."""
    try:
        return _batch_date(cfg, task)
    except PickerError:
        return None


def batch_total(task: PickTask) -> int:
    """Сколько единиц раскладывать по партиям.

    У ПЭУ с партиями — число марок (каждая марка — единица партии), у
    остальных — количество позиции, как его ждёт sapBatch.
    """
    return task.marks if task.kind == "MARK_PEU_BATCH" else task.count


def _pick_manufacturer(client: PickingClient, order_id: str, task: PickTask) -> dict:
    """Производитель партии: предпочтительный из конфига, иначе первый с кодом."""
    found = [m for m in client.sap_manufacturer(order_id, task.sku) if m.get("code")]
    if not found:
        raise PickerError("sapManufacturer",
                          f"для sku {task.sku} не вернулось ни одного производителя с кодом")
    preferred = client.cfg.preferred_manufacturer
    ordered = sorted(found, key=lambda m: m.get("name") != preferred)
    chosen = ordered[0]
    log.info("Производитель %s: %s", task.sku, chosen.get("name") or chosen.get("code"))
    return chosen


def _batch_not_found(exc: PickerError, task: PickTask, specs: list[BatchSpec]) -> PickerError:
    return PickerError(
        exc.method,
        f"{exc.reason or exc}\nSAP не нашёл партию для sku {task.sku} ({task.name}): "
        + "; ".join(str(s) for s in specs)
        + ". Проверьте производителя и дату производства и соберите товар заново",
        payload=exc.payload,
    )


def _do_batches(client: PickingClient, order_id: str, task: PickTask) -> None:
    """Партии SAP. У MARK_PEU_BATCH — sapBatchAdd на каждую, иначе один sapBatch."""
    specs = client.decisions.batches(client, order_id, task)
    if specs and task.kind != "MARK_PEU_BATCH":
        try:
            client.sap_batches(order_id, task.sku, specs)
        except PickerError as exc:
            if "партия" not in str(exc).lower():
                raise
            raise _batch_not_found(exc, task, specs) from exc
        log.info("Партии %s: %s", task.sku, "; ".join(str(s) for s in specs))
        return

    manufacturer = _pick_manufacturer(client, order_id, task)
    code = str(manufacturer.get("code") or "")
    iso = _batch_date(client.cfg, task)

    try:
        if task.kind == "MARK_PEU_BATCH":
            year, month, day = iso.split("-")
            for index in range(task.batch_count):
                client.sap_batch_add(order_id, task.sku, code=code,
                                     date_dmy=f"{day}.{month}.{year}",
                                     inn=str(manufacturer.get("inn") or ""))
                log.info("Партия %s: заведена %s/%s", task.sku, index + 1, task.batch_count)
        else:
            client.sap_batch(order_id, task.sku, code=code, date_iso=iso, count=task.count)
    except PickerError as exc:
        # «Партия с указанными признаками не найдена» = дата или производитель
        # не те. Сам стенд не говорит, что именно подставлять
        if "партия" not in str(exc).lower():
            raise
        branch = "LO_BATCH_DATE_ADD" if task.kind == "MARK_PEU_BATCH" else "LO_BATCH_DATE_SAP"
        raise PickerError(
            "sapBatch",
            f"{exc.args[0] if exc.args else exc}\nSAP не нашёл партию для sku "
            f"{task.sku} ({task.name}) с датой {iso} и производителем "
            f"«{manufacturer.get('name') or code}». Задайте другую дату:\n"
            f'    LO_BATCH_DATES={{"{task.sku}": "ГГГГ-ММ-ДД"}}  — только этому товару\n'
            f"    {branch}=ГГГГ-ММ-ДД                — всей этой ветке",
            payload=exc.payload,
        ) from exc


def _marking_kind(client: PickingClient, task: PickTask) -> tuple[str, str]:
    """Товарная группа марки и откуда она взялась.

    Приоритет: ручная строка `LO_MARKING_TYPES[sku]` → `markType` товара из
    Goods → общая `LO_MARKING_TYPE`. Общая на весь заказ — последнее средство:
    из-за неё вода собиралась марками SEAFOOD, и только мягкость тестового
    стенда (он разбирает лишь короткую часть марки) прятала ошибку.
    """
    cfg = client.cfg
    manual = next((v for k, v in cfg.marking_types.items()
                   if normalize_sku(k) == task.sku), None)
    if manual:
        return manual, "LO_MARKING_TYPES"
    card = client.goods_card(task)
    mark_type = str((card or {}).get("markType") or "")
    if kind := kind_from_goods(mark_type):
        return kind, f"Goods, markType {mark_type}"
    if mark_type:
        log.warning("markType %s товара %s не сопоставлен ни с одним генератором — "
                    "беру LO_MARKING_TYPE=%s. Задайте группу: LO_MARKING_TYPES="
                    '{"%s": "<группа>"}', mark_type, task.sku, cfg.marking_type, task.sku)
    return cfg.marking_type, "LO_MARKING_TYPE"


def _set_quantity(client: PickingClient, order_id: str, task: PickTask) -> bool:
    """Доставляет количество после скана. False — доставлять нечего.

    Весовому шлём всегда: сколько граммов стенд вычитает из штрихкода, зависит
    от того, как он его разберёт, а orderUpdate задаёт вес прямо и однозначно.
    """
    if task.weight or task.units > 1:
        client.set_count(order_id, task)
        return True
    return False


def _do_marks(client: PickingClient, order_id: str, task: PickTask, *,
              batch: bool, count: int | None = None) -> None:
    """Снятие марок: по одной на единицу у ПЭУ, одна на позицию у ОСУ.

    Метод выбирается по типу позиции, а не по одному флагу:

    * ОСУ (isOsuMarkRequired) — sapMarking {sku, orderId, marking};
    * ПЭУ партионный (isBatchMarking) — itemBatchIncreaseByMarking;
    * ПЭУ обычный — itemIncreaseByMarking.

    Подтверждено записью сборки: sku 189454 с isOsuMarkRequired=true уходит
    в sapMarking, а 575098 с isBatchMarking=true — в itemBatchIncreaseByMarking.
    Раньше ОСУ шёл общей веткой itemIncreaseByMarking и марка не доезжала.
    """
    kind, source = _marking_kind(client, task)
    osu = task.kind.startswith("MARK_OSU")
    log.info("Группа марки %s (%s): %s — из %s, метод %s", task.sku, task.name,
             kind, source,
             "sapMarking" if osu else
             "itemBatchIncreaseByMarking" if batch else "itemIncreaseByMarking")
    marks = task.marks if count is None else count
    for index in range(marks):
        mark = generate_marking(task.barcode, kind)
        try:
            if osu:
                client.sap_marking(order_id, task.order_sku or task.sku, mark)
            elif batch:
                client.item_batch_increase_by_marking(order_id, mark)
            else:
                client.item_increase_by_marking(order_id, mark, task.stock_id)
        except PickerError as exc:
            if "короткую марку" not in str(exc):
                raise
            raise PickerError(
                exc.method,
                f"{exc.reason or exc}. Формат марки задаётся товарной группой, а для "
                f"sku {task.sku} ({task.name}) сейчас взята «{kind}». Укажите "
                f'верную: LO_MARKING_TYPES={{"{task.sku}": "<группа>"}}. '
                f"Группы перечислены в marking.MARKING_GENERATORS",
                payload=exc.payload,
            ) from exc
        log.info("Марка %s (%s): %s/%s", task.sku, kind, index + 1, marks)


def _prepare_weight_barcode(goods: GoodsClient, task: PickTask) -> None:
    """Подставляет задаче сканируемый ШК с граммовкой.

    У весового товара штрихкод строится из базового кода и веса, а базовый
    берётся в Goods: в самом заказе лежит обычный EAN, который сборщик не
    сканирует.
    """
    base = goods.weight_base_barcode(task.store_id, task.sku, marked=task.marks > 0)
    task.barcode = weight_barcode(base, task.count)

    issues = weight_barcode_issues(task.barcode, task.count)
    if issues:
        raise GoodsError(
            "goods/item",
            f"ШК весового {task.sku} собран неверно: {'; '.join(issues)}. "
            f"База {base}, вес {task.count} г",
        )
    log.info("Весовой %s: %s (%s г)", task.sku, task.barcode, task.count)


#: Группы марок алкоголя. Признака алкоголя в позиции orderGet нет, а группа
#: марки задаётся для пива всё равно — иначе марка не сгенерируется. «nabeer»
#: (безалкогольное) сюда не входит: оно не алкоголь.
ALCOHOL_MARKING_KINDS = frozenset({"beer"})


def is_alcohol(client: PickingClient, task: PickTask) -> bool:
    """Алкоголь ли позиция: `isAlcohol` из Goods, иначе — по группе марки.

    В orderGet признака нет, а Goods отдаёт его прямо. Группа `beer` —
    запасной путь на случай, когда Goods не ответил.
    """
    card = client.goods_card(task)
    if card is not None and "isAlcohol" in card:
        return bool(card["isAlcohol"])
    if not task.kind.startswith("MARK"):
        return False
    try:
        return resolve_marking_kind(_marking_kind(client, task)[0]) in ALCOHOL_MARKING_KINDS
    except ValueError:
        return False


def skips_batches(client: PickingClient, task: PickTask) -> bool:
    """Пропускать ли партии SAP: собственные нужды без ВСД, и не алкоголь.

    По эталону (раздел 8.7) партии не запрашиваются только в СН **без** ВСД.
    В СН с ВСД нужны и партия, и марки. Коллекция пропускала партии во всех
    заказах в СН (OWN_ROUTE), а признака ВСД не знала. Алкоголь — отдельно:
    у него партии нужны всегда.
    """
    if not client.is_own_needs():
        return False
    if is_alcohol(client, task):
        log.info("Собственные нужды, но %s (%s) — алкоголь: партии запрашиваются",
                 task.sku, task.name)
        return False
    if client.vsd_required is True:
        log.info("Собственные нужды, но по заказу нужен ВСД: партии %s (%s) "
                 "запрашиваются", task.sku, task.name)
        return False
    log.info("Собственные нужды без ВСД: партии %s (%s) не запрашиваются",
             task.sku, task.name)
    return True


def _add_batch(client: PickingClient, order_id: str, task: PickTask, spec: BatchSpec) -> None:
    """Одна партия ПЭУ через sapBatchAdd — дата там ДД.ММ.ГГГГ."""
    year, month, day = spec.date_iso.split("-")
    try:
        client.sap_batch_add(order_id, task.sku,
                             code=str(spec.manufacturer.get("code") or ""),
                             date_dmy=f"{day}.{month}.{year}",
                             inn=str(spec.manufacturer.get("inn") or ""))
    except PickerError as exc:
        if "партия" not in str(exc).lower():
            raise
        raise _batch_not_found(exc, task, [spec]) from exc
    log.info("Партия %s: %s", task.sku, spec)


def _dairy_picked(order: dict, task: PickTask) -> int:
    for original in order.get("itemsList") or []:
        for item in (original, original.get("replacement") or {}):
            if normalize_sku(item.get("sku")) != task.sku:
                continue
            records = (item.get("stockData") or {}).get("records") or [item]
            for record in records:
                if record is item or record.get("id") == task.stock_id:
                    return int(record.get("countPick") or 0)
    raise PickerError("sapOsuMarkingBatchAdd",
                      f"в ответе нет позиции {task.sku} на складе {task.stock_id}")


def _pick_dairy_batches(client: PickingClient, order_id: str, task: PickTask, barcode: str) -> None:
    total = task.ordered_count or task.count
    picked = total - task.count
    client.scan(task, order_id)
    while picked < total:
        mark = client.decisions.dairy_mark(task, barcode)
        info = client.batch_marking_info(order_id, mark)
        found = [m for m in client.sap_manufacturer(order_id, task.sku) if m.get("code")]
        if not found:
            raise PickerError("sapManufacturer", f"нет производителя для SKU {task.sku}")
        found.sort(key=lambda m: m.get("name") != client.cfg.preferred_manufacturer)
        raw_date = info.get("productionDate") or default_batch_date(client.cfg, task) or ""
        manufacturer, chosen_date = client.decisions.dairy_batch(found, raw_date[:10])
        try:
            production_date = date.fromisoformat(chosen_date)
        except ValueError as exc:
            raise PickerError("sapOsuMarkingBatchAdd",
                              f"нужна дата производства для SKU {task.sku}") from exc
        result = client.osu_batch_add(order_id, task, manufacturer, production_date, info)
        order = result.get("order", result)
        if not order.get("itemsList"):
            order = client.order_get(order_id)
        current = _dairy_picked(order, task)
        if not picked < current <= total:
            raise PickerError("sapOsuMarkingBatchAdd",
                              f"SKU {task.sku}: было собрано {picked}, стало {current} из {total}")
        picked = current


def _pick_task(client: PickingClient, order_id: str, task: PickTask,
               goods: GoodsClient | None = None) -> None:
    """Одна позиция очереди. Ветвление повторяет ROUTE из flow.js."""
    kind = task.kind
    marking_barcode = task.barcode
    if kind in ("SAMPLE", "SAMPLE_SKU"):
        # Проба не сканируется: количество проставляется напрямую
        client.set_sample(order_id, task)
        return

    if task.weight:
        # Весовому нужен ШК с граммовкой, и только потом обычная ветка
        _prepare_weight_barcode(goods or GoodsClient(client.cfg), task)

    if task.batch_marking_assembly is True:
        _pick_dairy_batches(client, order_id, task, marking_barcode)
        return
    if task.batch_marking_assembly is False and kind.startswith("MARK_OSU"):
        mark_type = task.mark_type or (client.goods_card(task) or {}).get("markType")
        if kind_from_goods(mark_type) == "milk":
            mark = client.decisions.dairy_mark(task, marking_barcode)
            client.sap_marking(order_id, task.order_sku or task.sku, mark)
            total = task.ordered_count or task.count
            client.set_count(order_id, replace(task, count=total, units=total))
            return

    # Решение о партиях считаем один раз: оно же пишет в журнал, почему
    own_route = kind in ("BATCH", "MARK_OSU_BATCH", "MARK_PEU_BATCH") \
        and skips_batches(client, task)

    if kind == "MARK_PEU":
        # Не сканируется: марки снимаются сразу, по одной на единицу
        _do_marks(client, order_id, task, batch=False)
        return

    client.scan(task, order_id)

    if kind == "SIMPLE":
        # Скан кладёт одну единицу, остальное доставляется количеством
        _set_quantity(client, order_id, task)
    elif kind == "MARK_OSU" or (kind == "MARK_OSU_BATCH" and own_route):
        # В собственные нужды партионный ОСУ идёт ровно маршрутом обычного:
        # OWN_ROUTE ведёт скан -> марка -> @osuQuantity, минуя партии.
        #
        # У ОСУ единицу кладёт не первый скан и не марка, а ПОВТОРНЫЙ скан ШК
        # после sapMarking — так делает приложение (запись Charles по заказу
        # 900001126466: скан → countPick null, марка → null, скан → 1).
        # Поэтому повторный скан нужен всегда, а количество сверх одной
        # единицы доставляется уже после него
        _do_marks(client, order_id, task, batch=False)
        client.scan(task, order_id)
        _set_quantity(client, order_id, task)
    elif kind == "BATCH":
        # В собственные нужды партии не запрашиваются — только количество
        if own_route:
            _set_quantity(client, order_id, task)
        else:
            _do_batches(client, order_id, task)
    elif kind == "MARK_OSU_BATCH":
        _do_marks(client, order_id, task, batch=False)
        _do_batches(client, order_id, task)
    elif kind == "MARK_PEU_BATCH":
        # В собственные нужды sapBatchAdd пропускается, сразу марки
        specs = None if own_route else client.decisions.batches(client, order_id, task)
        if specs:
            # Несколько партий — по очереди: партия, затем её марки. Иначе SAP
            # ответит «только одна несобранная партия может быть без ИНН»
            for spec in specs:
                _add_batch(client, order_id, task, spec)
                _do_marks(client, order_id, task, batch=True, count=spec.count)
        else:
            if not own_route:
                _do_batches(client, order_id, task)
            _do_marks(client, order_id, task, batch=True)


def _start_assembly(client: PickingClient, order_id: str, plan: PickingPlan) -> None:
    """Уточняет маршрут по позиции из ответа на начало сборки.

    orderGet даёт остатки по складам, но может пропустить isBatchMarkingAssembly.
    В orderUpdate флаг приходит уже после начала сборки.
    """
    result = client.set_status(order_id, "ASSEMBLE_INCOMPLETE")
    updated = (result or {}).get("order") or {}
    flags = {}
    for original in updated.get("itemsList") or []:
        for item in (original, original.get("replacement") or {}):
            flag = item.get("isBatchMarkingAssembly")
            if item.get("sku") and isinstance(flag, bool):
                flags[normalize_sku(item["sku"])] = flag
    for task in plan.tasks:
        if task.sku in flags:
            task.batch_marking_assembly = flags[task.sku]
            if task.batch_marking_assembly:
                task.kind = "MARK_OSU_BATCH"


def _assemble_items(client: PickingClient, order: dict, order_id: str,
                    allow_partial: bool, resume: bool) -> None:
    plan = build_plan(order, resume=resume)
    if plan.unsupported and not allow_partial:
        raise PickerError(
            "orderGet",
            "в заказе есть позиции, которые скрипт собирать не умеет:\n    "
            + "\n    ".join(plan.unsupported)
            + "\nЗапустите с --allow-partial, чтобы собрать остальное, "
              "или закажите штучный немаркированный товар",
        )
    for line in plan.picked:
        log.info("Уже собрано: %s", line)

    if not plan.tasks:
        if not resume:
            raise PickerError("orderGet", f"в заказе {order_id} нечего собирать")
        # Всё собрано предыдущим прогоном — пересобирать нельзя: SAP ответит
        # «только одна несобранная партия может быть без ИНН»
        log.info("Заказ %s собран полностью — перехожу к проверке комплектации",
                 order_id)
    else:
        client.payment_check(order_id)
        _start_assembly(client, order_id, plan)
        removed: set[str] = set()
        for task in plan.tasks:
            if task.sku in removed:
                continue  # позиция разложена по складам, убрана целиком
            remaining = task.count
            reason = client.decisions.remove_reason(task)
            if reason is not None:
                client.remove_item(order_id, task, reason)
                removed.add(task.sku)
                continue
            if task.count < remaining:
                client.partial_targets[task.position_key] = task
            _pick_task(client, order_id, task)
            log.info("Собрано: %s", task)

        # Причина относится ко всей позиции, даже если собирали с нескольких складов.
        shortages = {t.order_sku or t.sku: t.shortage_reason for t in plan.tasks
                     if t.shortage_reason and t.sku not in removed}
        for sku, reason_id in shortages.items():
            client.set_missing_reason(order_id, sku, reason_id)
            log.info("Недобор %s: передана причина %s", sku, reason_id)

    # Валидация в SAP нужна любому заказу, а не только партионному: без неё
    # переход в PACKAGING отвергается — «заказ не отправлен в SAP»
    _validate_sap(client, order_id)


def _status_text(order: dict) -> str:
    info = order.get("statusInfo") or {}
    return str(info.get("text") or info.get("description") or info.get("alias") or "—")


def _fix_after_sap_error(client: PickingClient, order: dict, order_id: str) -> None:
    """Обработка SAP_VALIDATING_ERROR по эталону (раздел 10.3).

    Ошибка по марке или партии (ET_PROBLEM_MARK / ET_PROBLEM_PROD): система
    очищает проблемные марки, позиция возвращается в комплектацию — значит,
    её `countPick` уменьшается. Такие позиции пересобираются с новыми марками.
    Ошибка без списков (блокировка задания и т. п.): пересобирать нечего,
    заказ отправляется на проверку целиком заново.
    """
    log.warning("Валидация SAP заказа %s: ошибка — %s", order_id, _status_text(order))
    plan = build_plan(order, resume=True, targets=client.partial_targets)
    if plan.tasks:
        log.info("Возврат в комплектацию: пересобираю %s",
                 ", ".join(str(t) for t in plan.tasks))
        try:
            _start_assembly(client, order_id, plan)
        except PickerError as exc:
            # Стенд мог уже вернуть заказ в комплектацию сам
            log.info("ASSEMBLE_INCOMPLETE не выставлен (%s) — пересобираю как есть", exc)
        for task in plan.tasks:
            _pick_task(client, order_id, task)
            log.info("Пересобрано: %s", task)
    else:
        log.info("Проблемных позиций не видно — отправляю заказ на проверку целиком заново")
    client.sap_validate(order_id)


def _validate_sap(client: PickingClient, order_id: str) -> None:
    """Гонит заказ через валидацию SAP до SAP_VALIDATING_SUCCESS.

    Цикл из коллекции: прочитать статус → не успех, значит снова `sapValidate`
    и пауза → прочитать снова. Одного запуска не хватает: валидация
    асинхронная, и промежуточный `SAP_VALIDATING` означает «ещё идёт», а не
    «скоро закончится сама».

    `SAP_VALIDATING_ERROR` не останавливает прогон: проблемное пересобирается
    и заказ уходит на проверку снова (`_fix_after_sap_error`), не более
    LO_SAP_ERROR_RETRIES раз.
    """
    attempts = max(1, client.cfg.sap_validate_attempts)
    fixes = 0
    for attempt in range(1, attempts + 1):
        order = client.order_get(order_id)
        alias = status_of(order).upper()
        if alias == "SAP_VALIDATING_SUCCESS":
            log.info("Валидация SAP пройдена (чтений: %s)", attempt)
            # Проверка из коллекции: методы могли не ругнуться, а товар в заказ
            # не попасть — тогда «зелёный» прогон ничего не доказывает
            issues = picked_issues(order, targets=client.partial_targets)
            if issues:
                raise PickerError(
                    "orderGet",
                    f"валидация прошла, но заказ {order_id} собран не полностью: "
                    + "; ".join(issues),
                )
            return
        if alias == "SAP_VALIDATING_ERROR":
            fixes += 1
            if fixes > client.cfg.sap_error_retries:
                raise PickerError(
                    "orderGet",
                    f"валидация SAP заказа {order_id} снова завершилась ошибкой "
                    f"({_status_text(order)}) — исправить не удалось за "
                    f"{client.cfg.sap_error_retries} попытки. Проблема, видимо, не в "
                    f"марках: проверьте заказ в SAP или исключите проблемный товар",
                )
            _fix_after_sap_error(client, order, order_id)
            if attempt < attempts:
                time.sleep(client.cfg.sap_validate_delay)
            continue

        log.info("Статус %s — запускаю sapValidate (%s/%s)",
                 alias or "—", attempt, attempts)
        client.sap_validate(order_id)
        if attempt < attempts:
            time.sleep(client.cfg.sap_validate_delay)

    raise PickerError(
        "sapValidate",
        f"SAP не завершил валидацию заказа {order_id} за {attempts} чтений. "
        f"Поднимите LO_SAP_VALIDATE_ATTEMPTS или LO_SAP_VALIDATE_DELAY, "
        f"либо проверьте заказ в TMS",
    )


def pick_order(client: PickingClient, order_id: str, *,
               allow_partial: bool = False, until: str = "complete") -> str:
    """Проводит заказ сборщиком от его текущего статуса до ASSEMBLE_COMPLETE.

    Возвращает точку, на которой заказ был найден: по ней видно, что прогон
    не начинал сборку заново, а продолжил незаконченную.

    `until="validated"` останавливает после проверки комплектации в SAP — так
    дособирается чужой открытый заказ, чтобы освободить сборщика.
    """
    # Признаки СН и ВСД — свои у каждого заказа. Один клиент сборщика может
    # собрать подряд несколько (чужой открытый, потом свой), поэтому они
    # сбрасываются и отслеживаются только на время этой сборки
    client.partial_targets.clear()
    client.own_needs, client.vsd_required = False, None
    client.tracks_assembly_flags = True
    try:
        return _pick_order(client, order_id, allow_partial=allow_partial, until=until)
    finally:
        client.tracks_assembly_flags = False


def _pick_order(client: PickingClient, order_id: str, *,
                allow_partial: bool, until: str) -> str:
    order = client.order_get(order_id)
    order_id = str(order.get("hubOrderId") or order_id)
    action, why = resume_point(order)
    # Самовывоз курьеру не отдаётся: его флоу кончается на ASSEMBLE_COMPLETE,
    # поэтому «скомплектован» для него означает «готово», а не «дальше курьер»
    if action == "delivery" and is_pickup_order(order):
        action, why = "done", "самовывоз скомплектован — доставки нет"
    log.info("Заказ %s в статусе %s: %s", order_id, status_of(order) or "—", why)

    if action == "stop":
        raise PickerError("orderGet", f"заказ {order_id}: {why}")
    if action in ("done", "delivery", "waiting", "delivered"):
        return action  # сборка уже позади, дальше дело курьера

    if action == "assemble":
        # ASSEMBLE_INCOMPLETE значит, что сборку уже начинали: берём только
        # недостающее, иначе собранные позиции пойдут по второму кругу
        _assemble_items(client, order, order_id, allow_partial,
                        resume=status_of(order).upper() == "ASSEMBLE_INCOMPLETE")
        action = "packaging"
    if action in ("poll", "fix"):
        # Валидацию начал прошлый прогон — дожимаем тем же циклом. Он же
        # разбирает SAP_VALIDATING_ERROR: пересобирает и отправляет заново
        _validate_sap(client, order_id)
        action = "packaging"
    if until == "validated":
        log.info("Заказ %s дособран и прошёл проверку в SAP", order_id)
        return "validated"
    if action == "packaging":
        client.sku_groups(order_id)
        client.set_status(order_id, "PACKAGING")
        action = "packages"
    if action == "packages":
        # getPackages только читает список типов упаковки и ничего не меняет.
        # Сервис упаковки на стенде регулярно недоступен, и ронять из-за него
        # уже собранный заказ незачем — до ASSEMBLE_COMPLETE доходим без него
        try:
            client.packages(order_id)
        except PickerError as exc:
            # Общий совет «дождитесь, пока поднимут» тут вреден: сервис упаковки
            # на стенде не поднят никогда. Ответ стенда — только в файл лога
            log.debug("getPackages: %s", exc.reason or exc)
            log.warning("getPackages недоступен (%s) — продолжаю без него",
                        "сервис упаковки на стенде не поднят, это ожидаемо"
                        if exc.unreachable else exc.reason or exc)
        client.set_status(order_id, "ASSEMBLE_COMPLETE")

    log.info("Заказ %s собран", order_id)
    return "assembled"


def deliver_order(client: PickingClient, order_id: str) -> None:
    """Доставка курьером от текущего статуса до DELIVERED.

    Самовывоз курьеру не отдаётся: его флоу кончается на ASSEMBLE_COMPLETE.
    """
    # Собственные нужды и ВСД влияют только на партии при сборке — курьеру
    # следить за ними незачем
    client.tracks_assembly_flags = False
    client.delivery_tasks()
    order = client.order_get(order_id)

    if is_pickup_order(order):
        log.info("Заказ %s — самовывоз, шаги курьера пропускаются", order_id)
        return

    alias = status_of(order).upper()
    if alias in FINAL_STATUSES:
        log.info("Заказ %s уже доставлен (%s)", order_id, alias)
        return
    # Продолжаем с шага, следующего за текущим статусом
    start = DELIVERY_STATUSES.index(alias) + 1 if alias in DELIVERY_STATUSES else 0
    for status in DELIVERY_STATUSES[start:]:
        result = client.set_status(order_id, status)
        got = status_of(result.get("order") or {}).upper()

        # После DELIVERED заказ B2B с оплатой по расчётному счёту сразу уезжает
        # в FINISHED — довзыскивать нечего. На контурах с постоплатой остаётся
        # в DELIVERED, поэтому принимаем оба
        expected = FINAL_STATUSES if status == "DELIVERED" else {status}
        if got and got not in expected:
            raise PickerError(
                "orderUpdate",
                f"заказ {order_id} не перешёл в {status}: статус остался {got}. "
                f"Дальше идти нельзя — следующий шаг его не примет",
            )
        log.info("Заказ %s: %s", order_id, got or status)
