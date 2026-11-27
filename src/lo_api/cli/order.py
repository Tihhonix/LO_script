"""lo-order — быстрое оформление заказа в обход веб-морды.

Основной инструмент. Тесты — надстройка над теми же клиентами.

    lo-order                                   # спросит недостающее
    lo-order --mission pickup --sku 433217:2 --yes
    lo-order --token <sessionToken> --sku 433217   # без авторизации вообще
    lo-order --dry-run                         # собрать корзину, заказ не создавать

Авторизация выбирается по первому доступному источнику:
  1. --token / LO_SESSION_TOKEN   — готовый токен, авторизация пропускается
  2. кэш .lo_session.json         — токен прошлого запуска (снимает лимит на код)
  3. вход по коду                 — --code / LO_SMS_STUB_CODE / запрос в консоли
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

from lo_api import last_order
from lo_api.cli.steps import (
    BACK_LABEL,
    BACK_WORDS,
    Back,
    Step,
    Stop,
    run_steps,
)
from lo_api.clients import LentaApi
from lo_api.config import Account, Settings, available_stands, load_settings
from lo_api.constants import AddressKind, Mission
from lo_api.flows import CheckoutFlow, CheckoutParams
from lo_api.goods import GoodsClient, find_barcodes, piece_barcodes
from lo_api.http.errors import LoApiError
from lo_api.http.transport import Context
from lo_api.marking import (
    GS,
    generate_marking,
    kind_from_goods,
    looks_like_barcode,
    marking_groups,
    resolve_marking_kind,
    to_marking_barcode,
)
from lo_api.models import Address, Cart, Interval, Order, Organization, PastOrder
from lo_api.picker import PickerError, PickerOrderNotFound
from lo_api.picking import (
    FALLBACK_CANCEL_REASON,
    BatchSpec,
    OpenOrderError,
    PickDecisions,
    PickingClient,
    PickTask,
    RouteBlockedError,
    assigned_performers,
    assignment_issues,
    batch_total,
    default_batch_date,
    deliver_order,
    is_pickup_id,
    is_pickup_order,
    normalize_sku,
    pick_order,
)
from lo_api.session_cache import CachedSession
from lo_api.session_cache import clear as clear_cache
from lo_api.session_cache import load as load_cache
from lo_api.session_cache import save as save_cache
from lo_api.tms import Performer, TmsClient, assignment_confirmed
from lo_api.workshift import WorkshiftClient

log = logging.getLogger("lo_api.order")


# --- ввод -------------------------------------------------------------------
def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask(prompt: str, default: str = "", *, allow_back: bool = False) -> str:
    """Строковый ввод. С allow_back «<» или «назад» возвращает на прошлый шаг."""
    if not _interactive():
        return default
    suffix = f" [{default}]" if default else ""
    hint = "  («<» — назад)" if allow_back else ""
    raw = input(f"{prompt}{suffix}{hint}: ").strip()
    if allow_back and raw.lower() in BACK_WORDS:
        raise Back
    return raw or default


def _choose(title: str, options: list[str], default: int = 0, *,
            allow_back: bool = False) -> int:
    """Выбор из списка. С allow_back в конец добавляется пункт возврата.

    Возврат — обычный пункт меню, а не скрытая комбинация: человек видит, что
    выбор можно переиграть, не читая документацию.
    """
    if not _interactive():
        return default
    shown = [*options, BACK_LABEL] if allow_back else list(options)
    print(f"\n{title}")
    for i, opt in enumerate(shown, start=1):
        mark = "  (по умолчанию)" if i - 1 == default else ""
        print(f"  {i}. {opt}{mark}")
    while True:
        raw = input(f"Номер [{default + 1}]: ").strip()
        if not raw:
            return default
        if allow_back and raw.lower() in BACK_WORDS:
            raise Back
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            picked = int(raw) - 1
            if allow_back and picked == len(shown) - 1:
                raise Back
            return picked
        print("Введите номер из списка.")


def _looks_like_decimal_tail(previous: str, current: str) -> bool:
    """Похоже ли, что запятую написали как десятичный знак, а не разделитель.

    '433217:1,5' → после запятой короткое число без двоеточия, а перед ней было
    количество. Молча разобрать нельзя: '5' стало бы отдельным товаром.
    '433217:2, 555:3' — обычный список, двоеточие во втором элементе.
    """
    return ":" in previous and ":" not in current and current.isdigit() and len(current) <= 2


def parse_sku(values: list[str]) -> list[tuple[str, float]]:
    """'433217:2' → ('433217', 2.0). Количество по умолчанию — 1.

    Запятая разделяет позиции: '433217:2, 555:3'.
    Дробное количество пишется через точку: '433217:1.5'.
    """
    items: list[tuple[str, float]] = []
    for raw in values:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        for prev, cur in pairwise(parts):
            if _looks_like_decimal_tail(prev, cur):
                raise SystemExit(
                    f"Ошибка в {raw!r}: запятая разделяет позиции, а дробное количество "
                    f"пишется через точку — например '433217:1.5'. Если {cur!r} — это "
                    f"отдельный товар, передайте его своим --sku."
                )
        for part in parts:
            sku, _, qty = part.partition(":")
            sku = sku.strip()
            if not sku.isdigit():
                raise SystemExit(f"Ошибка: SKU {sku!r} должен состоять только из цифр")
            try:
                quantity = float(qty.strip() or "1")
            except ValueError:
                raise SystemExit(f"Ошибка: количество {qty!r} не число") from None
            if quantity <= 0:
                raise SystemExit(f"Ошибка: количество для {sku} должно быть больше нуля")
            items.append((sku, quantity))
    return items


# --- авторизация ------------------------------------------------------------
def choose_phone(cfg: Settings, args: argparse.Namespace) -> str:
    """Аккаунт, под которым работаем: выбор из LO_ACCOUNTS или свой номер.

    Номер не зашит нигде — ни в коде, ни умолчанием в вопросе. Частые
    учётки лежат списком в .env, разовую можно ввести прямо в меню.
    """
    if args.phone:
        return Account(phone=args.phone).phone

    accounts = cfg.accounts
    if not _interactive() or args.yes:
        return accounts[0].phone if accounts else ""

    # Список из LO_ACCOUNTS и последним пунктом — свой номер. Без аккаунтов
    # в конфиге меню не из чего строить, сразу спрашиваем номер
    if accounts:
        labels = [f"{a.label} — {a.phone}" if a.label else a.phone for a in accounts]
        choice = _choose("Аккаунт:", [*labels, "ввести номер вручную"])
        if choice < len(accounts):
            return accounts[choice].phone

    while True:
        raw = _ask("Номер телефона")
        if not raw:
            print("  нужен номер: 11 цифр с 7 или 8, либо 10 без кода страны")
            continue
        try:
            return Account(phone=raw).phone
        except ValueError as exc:
            print(f"  {exc}")


def authenticate(api: LentaApi, cfg: Settings, args: argparse.Namespace) -> str:
    """Возвращает телефон, под которым работаем (для журнала и кэша)."""
    phone = choose_phone(cfg, args)

    # 1. Готовый токен — аварийный обход авторизации, а не основной путь.
    # Живость проверяется: протухший токен из .env раньше уводил прогон
    # в невнятное падение где-то на середине флоу.
    token = args.token or cfg.session_token
    if token:
        api.auth.use_token(token, args.passport_id or cfg.passport_id)
        if api.auth.is_session_alive():
            return phone or "(токен)"
        if args.token:
            raise SystemExit(
                "Переданный --token недействителен. Возьмите свежий из DevTools "
                "(кука Utk_SssTkn) или уберите флаг — тогда скрипт войдёт по коду."
            )
        print("Токен из LO_SESSION_TOKEN протух — вхожу заново.")
        api.ctx.session_token = None
        api.ctx.passport_id = None

    if not phone:
        raise SystemExit(
            "Не указан номер телефона: введите его в ответ на вопрос, передайте "
            "--phone или заполните LO_ACCOUNTS в .env."
        )

    # 2. Кэш прошлого запуска
    if cfg.use_token_cache and not args.fresh:
        cached = load_cache(cfg.token_cache_path, phone=phone, max_age=cfg.token_max_age)
        if cached:
            api.auth.use_token(cached.token, cached.passport_id)
            api.ctx.legal_entity_id = cached.legal_entity_id or None
            if cached.device_id:
                api.ctx.extra["device_id"] = cached.device_id
            if api.auth.is_session_alive():
                print(f"✓ Использую сохранённую сессию ({cached})")
                return phone
            print("Сохранённая сессия протухла — вхожу заново.")
            clear_cache(cfg.token_cache_path)
            api.ctx.session_token = None
            api.ctx.passport_id = None
            api.ctx.legal_entity_id = None

    # 3. Вход по коду
    code = args.code or cfg.sms_stub_code
    if not code:
        api.auth.request_code(phone)  # терпим к «код уже запрошен»
        code = _ask("Код из СМС")
        if not code:
            raise SystemExit(
                "Код не введён. Варианты: --code <код>, LO_SMS_STUB_CODE в .env, "
                "или --token <sessionToken> чтобы обойти авторизацию."
            )
    api.auth.login(phone, code)

    if cfg.use_token_cache:
        save_cache(cfg.token_cache_path, CachedSession(
            phone=phone,
            token=api.ctx.session_token or "",
            passport_id=api.ctx.passport_id or "",
            device_id=api.t.device_id,
        ))
    return phone


def choose_organization(api: LentaApi, args: argparse.Namespace, *,
                        allow_back: bool = False) -> Organization:
    orgs = api.b2b.organizations()
    if not orgs:
        raise SystemExit("У аккаунта нет доступных организаций.")
    if args.org:
        needle = args.org.lower()
        if needle.isdigit() and 1 <= int(needle) <= len(orgs):
            chosen = orgs[int(needle) - 1]
        else:
            matches = [o for o in orgs if needle in o.name.lower() or needle == o.inn]
            if not matches:
                raise SystemExit(
                    f"Организация {args.org!r} не найдена. Доступны: "
                    + "; ".join(o.name for o in orgs)
                )
            chosen = matches[0]
    else:
        # Список показывается и при одной организации: под какой оформляется
        # заказ — не та вещь, которую стоит выбирать за человека молча.
        chosen = orgs[_choose("Организация:", [str(o) for o in orgs],
                              allow_back=allow_back)]
    api.b2b.select(chosen)
    return chosen


def _interval_label(interval: Interval) -> str:
    if interval.time_from and interval.time_to:
        when = f"{interval.time_from[:5]}–{interval.time_to[:5]}"
    else:
        when = str(interval)
    return f"{when}  {interval.price} ₽" if interval.price else when


def choose_interval(intervals: list[Interval], args: argparse.Namespace, *,
                    allow_back: bool = False) -> Interval:
    """Интервал: по Id, интерактивно или по индексу среди доступных.

    Интервалов приходит под сотню на несколько дней вперёд, поэтому выбор
    двухступенчатый — сначала дата, потом время внутри неё.
    """
    available = [i for i in intervals if i.is_available]
    if not available:
        raise SystemExit(f"Нет доступных интервалов (всего в ответе: {len(intervals)}).")

    if args.interval_id:
        found = next((i for i in available if str(i.id) == args.interval_id), None)
        if found is None:
            raise SystemExit(
                f"Интервал {args.interval_id!r} недоступен. Доступные Id: "
                + ", ".join(str(i.id) for i in available[:5])
                + (f" … и ещё {len(available) - 5}" if len(available) > 5 else "")
            )
        return found

    if getattr(args, "today_only", False):
        # Повтор заказа по номеру — на сегодня. Если сегодня всё занято,
        # честно говорим и берём ближайший день, а не падаем
        today = date.today().isoformat()
        todays = [i for i in available if i.date == today]
        if todays:
            available = todays
        else:
            print(f"  на сегодня ({today}) свободных интервалов нет — беру ближайший день")

    repeat_time = getattr(args, "repeat_time", None)
    if repeat_time is not None:
        # Повтор: вчерашний слот уже недоступен, берём то же время в ближайший
        # день, где оно есть, а если нигде нет — ближайший доступный. Время
        # сравнивается без секунд: в заказе и интервалах оно бывает «12:00:00»
        same = next((i for i in available
                     if (i.time_from or "")[:5] == repeat_time[:5]), None)
        if same is None:
            print(f"  интервала на {repeat_time} нет ни в один день — беру ближайший")
        return same or available[0]

    if not _interactive() or args.yes:
        return available[min(args.interval, len(available) - 1)]

    dates = sorted({i.date for i in available if i.date})
    if len(dates) > 1:
        picked = dates[_choose("Дата:", dates, allow_back=allow_back)]
        available = [i for i in available if i.date == picked]
    return available[_choose("Время:", [_interval_label(i) for i in available],
                             allow_back=allow_back)]


def _pick_performer(title: str, performers: list[Performer],
                    requested: str | None, args: argparse.Namespace) -> Performer:
    """Исполнитель: по Id/имени, выбором из списка или первый настоящий."""
    if not performers:
        raise SystemExit(f"{title}: в форме TMS нет ни одного варианта.")
    if requested:
        found = next((p for p in performers
                      if p.id == requested or requested.lower() in p.title.lower()), None)
        if found is None:
            raise SystemExit(f"{title} {requested!r} не найден среди вариантов TMS.")
        return found
    if not _interactive() or args.yes:
        return performers[0]
    return performers[_choose(f"{title}:", [str(p) for p in performers])]


def choose_staff(cfg: Settings, role: str, title: str,
                 requested: str | None, args: argparse.Namespace) -> None:
    """Выбирает учётку сотрудника и делает её текущей.

    Под выбранной пойдёт не только смена, но и сборка с доставкой: заказ
    назначается на того, кто на смене, и работать он должен под тем же логином.
    Когда учётка одна, вопроса нет.
    """
    staff = getattr(cfg, f"{role}s")
    if len(staff) < 2 and not requested:
        return

    if requested:
        found = next((s for s in staff
                      if s.login == requested or requested.lower() in s.label.lower()), None)
        if found is None:
            raise SystemExit(
                f"{title} {requested!r} не найден среди учёток стенда {cfg.stand}: "
                f"{', '.join(str(s) for s in staff)}"
            )
    elif not _interactive() or args.yes:
        found = staff[0]
    else:
        found = staff[_choose(f"{title} — под какой учёткой работать?",
                              [str(s) for s in staff])]
    cfg.use_staff(role, found)


def open_workshift(cfg: Settings, args: argparse.Namespace) -> None:
    """Выводит сборщика и курьера на смену."""
    choose_staff(cfg, "picker", "Сборщик", args.picker, args)
    choose_staff(cfg, "courier", "Курьер", args.courier, args)
    shift = WorkshiftClient(cfg)
    try:
        print(f"  сборщик {shift.start_picker()} — на смене")
        print(f"  курьер {shift.start_courier()} — на смене")
    finally:
        shift.close()


def login_picker(cfg: Settings) -> PickingClient:
    """Вошедший клиент сборщика. Закрывать его — дело вызывающего."""
    picker = PickingClient(cfg)
    try:
        picker.require_credentials(cfg.picker_login, cfg.picker_password, "LO_PICKER")
        picker.login(cfg.picker_login, cfg.picker_password)
    except BaseException:
        picker.close()
        raise
    return picker


KIND_LABELS = {
    "SIMPLE": "штучный", "BATCH": "партионный",
    "MARK_PEU": "маркированный, марка на каждую единицу (ПЭУ)",
    "MARK_OSU": "маркированный, одна марка на позицию (ОСУ)",
    "MARK_PEU_BATCH": "маркированный ПЭУ с партиями",
    "MARK_OSU_BATCH": "маркированный ОСУ с партиями",
    "SAMPLE": "проба", "SAMPLE_SKU": "проба",
}


def choose_pick_mode(args: argparse.Namespace) -> str:
    """Как собирать: автоматически или с вопросами по каждому товару.

    Спрашивается один раз за прогон: если по ходу придётся дособирать чужой
    открытый заказ, он соберётся в том же режиме.
    """
    if args.pick_mode:
        if args.pick_mode == "ask" and not _interactive():
            raise SystemExit("✗ --pick-mode ask требует терминала: вопросы задать некому")
        return args.pick_mode
    if not _interactive() or args.yes:
        args.pick_mode = "auto"
    else:
        args.pick_mode = ["auto", "ask"][_choose(
            "Как собирать заказ?",
            ["Автоматически — партии и марки по настройкам стенда",
             "С вопросами по каждому товару — собрать или убрать, партии вручную"])]
    return args.pick_mode


def _parse_date(raw: str) -> str:
    """Дата производства в ISO. Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ."""
    text = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"не похоже на дату: {raw!r}. Формат ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def _int_between(low: int, high: int):
    def check(raw: str) -> None:
        if not raw.strip().isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"нужно целое число от {low} до {high}")
    return check


class AskDecisions(PickDecisions):
    """Сборка с вопросами: по каждому товару — собрать или убрать, партии вручную.

    Вопрос о партиях задаётся только там, где партии действительно нужны:
    в собственные нужды без ВСД их нет — нет и вопроса.
    """

    def remove_reason(self, task: PickTask) -> str | None:
        qty = (f"{task.count} г ({task.units} порц.)" if task.weight
               else f"{task.count} шт")
        print(f"\n── Товар {task.sku} «{task.name}»: {qty}, "
              f"{KIND_LABELS.get(task.kind, task.kind)}")
        options = ["Собрать полностью", "Убрать из заказа"]
        if task.count > 1 and not task.batch_marking_assembly:
            options.append("Собрать частично")
        elif task.count > 1:
            print("  Количество партии определяет SAP: sapOsuMarkingBatchAdd не принимает вес "
                  "или число единиц.")
        action = _choose("Что делаем с товаром?", options)
        if action == 2:
            unit = "г" if task.weight else "шт"
            count = int(_ask_until_valid(f"Сколько собрать из {task.count} {unit}",
                                         "1", _int_between(1, task.count - 1)))
            units = count
            if task.weight:
                units = 1
                if task.units > 1:
                    units = int(_ask_until_valid("Сколько порций в выбранном весе", "1",
                                                 _int_between(1, min(count, task.units))))
            task.select_count(count, units)
            task.shortage_reason = self._missing_reason(task, "Причина недобора")
            return None
        if action == 0:
            return None
        return self._missing_reason(task, "Причина уборки")

    @staticmethod
    def _missing_reason(task: PickTask, prompt: str) -> str:
        reasons = task.missing_reasons
        if not reasons:
            # Список причин приходит в позиции заказа; если его нет — вводим id
            return _ask_until_valid(f"{prompt} — id (списка причин в заказе нет)",
                                    "3", _int_between(1, 999))
        ids = list(reasons)
        return ids[_choose(prompt, [f"{str(reasons[i]).strip()} ({i})" for i in ids])]

    def dairy_mark(self, task: PickTask, barcode: str) -> str:
        mark = _ask(f"Марка молочной продукции {task.sku} (Enter — сгенерировать)")
        return mark or super().dairy_mark(task, barcode)

    def retry_batch_marking_info(self, error: PickerError) -> bool:
        if not _interactive():
            return False
        print(f"  {error}")
        return _choose("Повторить getBatchMarkingInfo с той же маркой?",
                       ["Повторить", "Завершить сборку с ошибкой"]) == 0

    def dairy_batch(self, manufacturers: list[dict], date_iso: str) -> tuple[dict, str]:
        labels = [f"{m.get('name') or '—'} ({m['code']})" for m in manufacturers]
        index = _choose("Производитель партии", labels) if len(labels) > 1 else 0
        value = _ask_until_valid("Дата производства партии", date_iso, _parse_date)
        return manufacturers[index], _parse_date(value)

    def batches(self, client: PickingClient, order_id: str,
                task: PickTask) -> list[BatchSpec]:
        found = [m for m in client.sap_manufacturer(order_id, task.sku) if m.get("code")]
        if not found:
            raise PickerError("sapManufacturer",
                              f"для sku {task.sku} не вернулось ни одного производителя с кодом")
        total = batch_total(task)
        unit = "г" if task.weight and task.kind != "MARK_PEU_BATCH" else "шт"
        count = int(_ask_until_valid(f"Сколько партий (всего {total} {unit})", "1",
                                     _int_between(1, total)))
        labels = [f"{m.get('name') or '—'} (код {m.get('code')}"
                  + (f", ИНН {m['inn']}" if m.get("inn") else "") + ")" for m in found]
        preferred = next((i for i, m in enumerate(found)
                          if m.get("name") == client.cfg.preferred_manufacturer), 0)
        date_default = default_batch_date(client.cfg, task) or ""

        specs: list[BatchSpec] = []
        left = total
        for index in range(count):
            print(f"  партия {index + 1} из {count}")
            if len(found) > 1:
                preferred = _choose("Производитель:", labels, default=preferred)
            else:
                print(f"  производитель: {labels[0]}")
            found_date: dict[str, str] = {}

            def check_date(raw: str, found_date=found_date) -> None:
                found_date["v"] = _parse_date(raw)

            _ask_until_valid("Дата производства (ГГГГ-ММ-ДД или ДД.ММ.ГГГГ)",
                             date_default, check_date)
            date_default = found_date["v"]

            if index == count - 1:
                qty = left
                if count > 1:
                    print(f"  количество: {qty} {unit} — остаток")
            else:
                most = left - (count - 1 - index)  # каждой следующей — хотя бы по одной
                qty = int(_ask_until_valid(f"Количество в партии, {unit}",
                                           str(max(1, left // (count - index))),
                                           _int_between(1, most)))
            left -= qty
            specs.append(BatchSpec(found[preferred], date_default, qty))
        return specs


def pick_resolving_open_order(picker: PickingClient, order_id: str,
                              args: argparse.Namespace) -> str:
    """Сборка, которая умеет разобраться с другим открытым заказом сборщика.

    Стенд не даёт собирать два заказа сразу: «Невозможно начать сборку
    нескольких заказов. Открытый заказ …». Мешающий заказ можно дособрать и
    отправить на проверку в SAP либо отменить — выбор за человеком, после
    него сборка начатого заказа продолжается.
    """
    handled: set[str] = set()
    while True:
        try:
            return pick_order(picker, order_id, allow_partial=args.allow_partial)
        except OpenOrderError as exc:
            other = exc.open_order_id
            if other in handled or other == str(order_id):
                raise SystemExit(
                    f"✗ Сборщик всё ещё занят заказом {other}, хотя с ним уже "
                    f"разобрались. Похоже, после проверки в SAP он остаётся "
                    f"открытым: доведите его до конца — lo-order --mode pick "
                    f"--order-id {other} — или отмените (--open-order cancel)."
                ) from exc
            resolve_open_order(picker, other, str(order_id), args)
            handled.add(other)
            print(f"\n── Возвращаюсь к заказу {order_id}")


def resolve_open_order(picker: PickingClient, other: str, current: str,
                       args: argparse.Namespace) -> None:
    """Дособрать и отправить на проверку либо отменить мешающий заказ."""
    print(f"\n  Сборщик {picker.cfg.picker_login} уже собирает заказ {other} — "
          f"начать сборку {current}, пока он открыт, стенд не даёт.")
    action = args.open_order
    if action is None:
        if not _interactive() or args.yes:
            raise SystemExit(
                f"✗ Открыт заказ {other}. Без диалога решать за вас, что с ним делать, "
                f"скрипт не станет — укажите --open-order finish (дособрать и "
                f"отправить на проверку) или --open-order cancel (отменить)."
            )
        action = ["finish", "cancel"][_choose(
            f"Что сделать с заказом {other}?",
            [f"Дособрать и отправить на проверку, затем вернуться к {current}",
             f"Отменить {other} и продолжить с {current}"])]

    if action == "finish":
        print(f"\n── Дособираю заказ {other}")
        pick_order(picker, other, allow_partial=args.allow_partial, until="validated")
        print(f"✓ Заказ {other} дособран и отправлен на проверку")
        return

    cancel_by_picker(picker, other, args)


def cancel_by_picker(picker: PickingClient, other: str, args: argparse.Namespace) -> None:
    """Отмена заказа сборщиком с причиной из списка стенда."""
    reasons = picker.cancel_reasons()
    if not reasons:
        reason = FALLBACK_CANCEL_REASON
        print(f"  список причин отмены не разобрался — беру причину {reason}, "
              f"как в запросе коллекции")
    elif _interactive() and not args.yes and len(reasons) > 1:
        reason = reasons[_choose("Причина отмены:",
                                 [f"{r['title']} ({r['id']})" for r in reasons])]["id"]
    else:
        reason = reasons[0]["id"]
        print(f"  причина отмены: {reasons[0]['title']} ({reason})")
    picker.cancel_order(other, reason)
    print(f"✓ Заказ {other} отменён")


def pick_and_deliver(cfg: Settings, order_id: str, args: argparse.Namespace,
                     picker: PickingClient | None = None) -> None:
    """Сборка сборщиком и доставка курьером — заказ доводится до финиша.

    Доставка идёт по умолчанию: собранный, но не доставленный заказ — это
    незавершённое дело, а не результат. Отключается флагом --no-deliver.
    У самовывоза финиш другой: ASSEMBLE_COMPLETE, курьеру его не отдают.

    Готовый `picker` принимается, чтобы не входить в picker-service дважды:
    режим сборки логинится раньше — проверить, назначен ли заказ.
    """
    own = picker is None
    picker = picker or login_picker(cfg)
    # Сборщик нужен до самого конца: если маршрут курьера держит чужой
    # несобранный заказ, дособирать или отменять его будет он
    try:
        picker.decisions = AskDecisions() if choose_pick_mode(args) == "ask" else PickDecisions()
        outcome = pick_resolving_open_order(picker, order_id, args)
        print(f"✓ Заказ {order_id} собран")
        # Миссию читаем сборщиком: иначе ради самовывоза пришлось бы входить
        # курьером, которому этот заказ всё равно не дадут
        if is_pickup_order(picker.order_get(order_id)):
            print("  самовывоз — сборкой заказ и завершается, курьер не нужен")
            return
        if outcome == "done":
            print("  заказ уже доставлен")
            return
        if args.deliver is False:
            print("  доставка пропущена (--no-deliver)")
            return
        deliver_resolving_route(cfg, picker, order_id, args)
    finally:
        if own:
            picker.close()


def deliver_resolving_route(cfg: Settings, picker: PickingClient, order_id: str,
                            args: argparse.Namespace) -> None:
    """Доставка, которая умеет разобраться с несобранным заказом в маршруте.

    Маршрут курьера уходит в доставку только целиком. Если TMS положил в него
    недособранный заказ из прошлых прогонов, стенд отвечает «Нельзя начать
    доставку маршрута при наличии несобранных заказов (orderId: …)» — и наш
    уже собранный заказ застревает в ASSEMBLE_COMPLETE.
    """
    courier = PickingClient(cfg)
    try:
        courier.require_credentials(cfg.courier_login, cfg.courier_password, "LO_COURIER")
        courier.login(cfg.courier_login, cfg.courier_password)
        handled: set[str] = set()
        while True:
            try:
                deliver_order(courier, order_id)
                break
            except RouteBlockedError as exc:
                other = exc.blocking_order_id
                if other in handled:
                    raise SystemExit(
                        f"✗ Маршрут всё ещё держит заказ {other}, хотя с ним уже "
                        f"разобрались. Проверьте его статус — lo-order --mode pick "
                        f"--order-id {other}"
                    ) from exc
                resolve_route_blocker(cfg, picker, other, order_id, args)
                handled.add(other)
                print(f"\n── Возвращаюсь к доставке заказа {order_id}")
        print(f"✓ Заказ {order_id} доставлен")
    finally:
        courier.close()


def resolve_route_blocker(cfg: Settings, picker: PickingClient, other: str, current: str,
                          args: argparse.Namespace) -> None:
    """Дособрать до конца либо отменить заказ, который держит маршрут."""
    print(f"\n  Маршрут курьера с заказом {current} не уходит в доставку: в нём "
          f"несобранный заказ {other}.")
    action = args.open_order
    if action is None:
        if not _interactive() or args.yes:
            raise SystemExit(
                f"✗ Маршрут держит несобранный заказ {other}. Без диалога решать за вас "
                f"не стану — укажите --open-order finish (дособрать) или "
                f"--open-order cancel (отменить)."
            )
        action = ["finish", "cancel"][_choose(
            f"Что сделать с заказом {other}?",
            [f"Дособрать до конца, затем доставить {current}",
             f"Отменить {other} и доставить {current}"])]

    if action == "cancel":
        cancel_by_picker(picker, other, args)
        return

    print(f"\n── Дособираю заказ {other}")
    try:
        # Маршрут ждёт именно собранный заказ, поэтому до ASSEMBLE_COMPLETE,
        # а не до проверки в SAP, как у открытого заказа сборщика
        ensure_assigned(cfg, picker, other, args)
        pick_resolving_open_order(picker, other, args)
    except PickerError as exc:
        raise SystemExit(
            f"✗ Дособрать заказ {other} не вышло: {exc}\n"
            f"Если он застрял (например, в SAP_VALIDATING_ERROR), отмените его: "
            f"--open-order cancel"
        ) from exc
    print(f"✓ Заказ {other} собран")


def assemble_order(cfg: Settings, order_id: str, args: argparse.Namespace,
                   picker_client: PickingClient | None = None) -> None:
    """Назначение сборщика и курьера — заказ уходит в сборку.

    Назначение проверяется по факту: TMS отвечает 200, даже когда ничего не
    применил. В заказе 900001126510 после «успешного» назначения сборщик
    остался пустым, курьер — заглушкой, и сборка упала на sapValidate:
    «не назначен b2b-курьер с указанным номером автомобиля».
    """
    # Самовывоз курьеру не отдаётся: требовать его в форме — значит не
    # назначить заказ вовсе. В самой форме для этого есть «Не переносить»
    pickup = is_pickup_id(order_id)

    def enough(page) -> bool:
        return bool(page.real_pickers and (pickup or page.real_couriers))

    tms = TmsClient(cfg)
    try:
        tms.login()
        page = tms.open_order(order_id)
        if args.open_shift or not enough(page):
            # В списках одни Dummy: значит смену никто не открыл и назначать
            # не на кого. Выводим сотрудников и перечитываем форму.
            print("  вывожу сборщика и курьера на смену"
                  if args.open_shift else
                  "  на смене никого нет — вывожу сборщика и курьера")
            open_workshift(cfg, args)
            page = tms.open_order(order_id)
            if not enough(page):
                raise SystemExit(
                    f"После вывода на смену назначать всё равно некого.\n"
                    f"  TMS для заказа {order_id} предлагает — {page.summary()}\n"
                    f"  на смену выведены — сборщик {cfg.picker_login}, "
                    f"курьер {cfg.courier_login}, ТК {cfg.workshift_tk}\n"
                    f"Если в списке пусто или одни заглушки, а смена открылась без "
                    f"ошибок, значит заказ относится к другому хабу: сверьте "
                    f"LO_WORKSHIFT_TK с магазином заказа. Если же в списке есть "
                    f"настоящие, но не ваши — логины в LO_PICKERS должны совпадать "
                    f"с их Id в TMS."
                )
        # Логин сотрудника — это его Id в TMS, поэтому назначаем на того, под
        # кем открыта смена: иначе заказ уйдёт первому попавшемуся из списка,
        # а собирать его будет другая учётка
        picker = _pick_performer("Сборщик", page.picker_choices,
                                 args.picker or cfg.picker_login, args)
        courier = None
        if not pickup:
            courier = _pick_performer("Курьер", page.courier_choices,
                                      args.courier or cfg.courier_login, args)
        attempts = max(1, cfg.tms_assign_attempts)
        for attempt in range(1, attempts + 1):
            notes = tms.assign(order_id, page, picker=picker, courier=courier)
            if assignment_confirmed(notes, needs_courier=courier is not None):
                break
            missing = _await_assignment(cfg, order_id, picker_client)
            if not missing:
                break
            if attempt < attempts:
                print(f"  TMS не применил назначение ({', '.join(missing)} не назначен) — "
                      f"отправляю форму ещё раз")
                page = tms.open_order(order_id)
        else:
            said = "; ".join(notes) if notes else "ничего — ни сообщения, ни ошибки"
            raise SystemExit(
                f"✗ TMS принял форму, но заказ {order_id} так и не назначен: не хватает — "
                f"{', '.join(missing)}. Попыток: {attempts}.\n"
                f"  TMS ответил: {said}\n"
                f"Сборку не начинаю: без курьера с машиной она упадёт на проверке в SAP "
                f"(«не назначен b2b-курьер с указанным номером автомобиля»). Проверьте "
                f"заказ в TMS вручную; если не назначается курьер — у него может не быть "
                f"транспорта на смене."
            )
        print(f"✓ Назначено: сборщик {picker}, "
              + (f"курьер {courier}" if courier else "курьер не нужен (самовывоз)"))
    finally:
        tms.close()


def _await_assignment(cfg: Settings, order_id: str,
                      picker_client: PickingClient | None) -> list[str]:
    """Ждёт, пока назначение дойдёт до заказа. Возвращает, кого так и нет.

    Источник правды — pickerStaffInfo / courierStaffInfo в orderGet, а не
    ответ формы TMS: та отвечает 200 при любом исходе.
    """
    own = picker_client is None
    client = picker_client or login_picker(cfg)
    try:
        missing: list[str] = []
        for check in range(1, max(1, cfg.tms_assign_checks) + 1):
            try:
                missing = assignment_issues(client.order_get(order_id))
            except PickerOrderNotFound:
                if check >= cfg.tms_assign_checks:
                    raise
            else:
                if not missing:
                    return []
            if check < cfg.tms_assign_checks:
                time.sleep(cfg.tms_assign_check_delay)
        log.warning("Назначение не дошло до заказа %s: нет — %s", order_id, ", ".join(missing))
        return missing
    finally:
        if own:
            client.close()


def _ask_order_id(args: argparse.Namespace) -> str:
    order_id = args.order_id or _ask("Номер заказа")
    if not order_id:
        raise SystemExit("Не указан номер заказа. Передайте --order-id <номер>.")
    name_log_after_order(order_id)
    return order_id


def run_shift_only(cfg: Settings, args: argparse.Namespace) -> int:
    """Режим «только смена»: вывести сборщика и курьера, заказ не трогать.

    Отдельный режим нужен потому, что смена — предусловие всего остального:
    пока её никто не открыл, в списках исполнителей TMS одни Dummy.
    """
    print("── Вывод на смену")
    open_workshift(cfg, args)
    return 0


def _ask_until_valid(prompt: str, default: str, check) -> str:
    """Спрашивает, пока ответ не пройдёт проверку. check поднимает ValueError."""
    while True:
        answer = _ask(prompt, default)
        try:
            check(answer)
            return answer
        except ValueError as exc:
            if not _interactive():
                raise SystemExit(f"✗ {exc}") from None
            print(f"  {exc}")


def _marking_barcode(cfg: Settings, item: str,
                     args: argparse.Namespace) -> tuple[str, str, str]:
    """Штрихкод под марку, SKU и markType из Goods (по штрихкоду — пусто)."""
    if looks_like_barcode(item):
        return to_marking_barcode(item), "", ""

    sku = normalize_sku(item)
    if cfg.goods_store_id is None:
        raise SystemExit(
            f"«{item}» похоже на SKU, а для поиска его штрихкода нужен магазин Goods: "
            f"задайте LO_GOODS_STORE_ID в env/{cfg.stand}.env или передайте штрихкод "
            f"напрямую: --item <штрихкод>"
        )
    goods = GoodsClient(cfg)
    try:
        card = goods.card(cfg.goods_store_id, sku)
    finally:
        goods.close()
    codes = piece_barcodes(find_barcodes(card) or [])
    mark_type = str(card.get("markType") or "")
    if not codes:
        raise SystemExit(
            f"у товара {sku} нет штучного штрихкода — только весовые. Марка на весовой "
            f"код не выпускается; передайте нужный штрихкод напрямую: --item <штрихкод>"
        )
    print(f"  {card.get('name') or sku}: штрихкоды {', '.join(codes)}"
          + (f"; markType {mark_type}" if mark_type else ""))
    chosen = codes[0]
    if len(codes) > 1 and _interactive() and not args.yes:
        chosen = codes[_choose("Для какого штрихкода марка?", codes)]
    return to_marking_barcode(chosen), sku, mark_type


def run_mark_only(cfg: Settings, args: argparse.Namespace) -> int:
    """Режим «марка»: товар на входе, готовые марки Datamatrix на выходе.

    Стенд для этого не нужен вовсе, если задан штрихкод; по SKU штрихкод
    берётся из Goods — без авторизации, как и при сборке весового.
    """
    print("── Генерация марки")
    try:
        return _run_mark(cfg, args)
    except ValueError as exc:
        # Неверная группа или штрихкод из флагов: без этого — трейсбек
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1


def _run_mark(cfg: Settings, args: argparse.Namespace) -> int:
    item = args.item or (parse_sku(args.sku)[0][0] if args.sku else "")
    if item:
        barcode, sku, mark_type = _marking_barcode(cfg, item, args)
    else:
        found: dict[str, tuple[str, str, str]] = {}

        def check(answer: str) -> None:
            if not answer:
                raise ValueError("нужен SKU или штрихкод")
            found["v"] = _marking_barcode(cfg, answer, args)

        _ask_until_valid("Товар — SKU или штрихкод", "", check)
        barcode, sku, mark_type = found["v"]

    # Тот же приоритет, что при сборке: флаг → ручная строка → Goods → общая
    manual = next((v for k, v in cfg.marking_types.items()
                   if sku and normalize_sku(k) == sku), None)
    default_kind = (args.marking_kind or manual or kind_from_goods(mark_type)
                    or cfg.marking_type)
    if args.marking_kind or not _interactive() or args.yes:
        kind = resolve_marking_kind(default_kind)
    else:
        print(f"  группы: {', '.join(marking_groups())}")
        kind = resolve_marking_kind(_ask_until_valid(
            "Группа марки", default_kind, resolve_marking_kind))

    count = args.count
    if count is None:
        count = int(_ask_until_valid("Сколько марок", "1", _positive_int)
                    if _interactive() and not args.yes else 1)
    _positive_int(str(count))

    marks = [generate_marking(barcode, kind) for _ in range(count)]
    print(f"\n✓ {kind}, штрихкод {barcode}" + (f", sku {sku}" if sku else ""))
    for index, mark in enumerate(marks, 1):
        print(f"  {index}. {mark.replace(GS, '<GS>')}")
        # Для тел запросов в Postman/Bruno: там марка подставляется в JSON,
        # и разделитель должен прийти именно escape-последовательностью
        print(f"     JSON: {json.dumps(mark)}")

    if args.copy:
        _copy_to_clipboard("\n".join(marks))
    else:
        print("\n  <GS> — невидимый разделитель U+001D. Из терминала он при копировании "
              "теряется;\n  берите строку JSON или запустите с --copy, чтобы марка "
              "попала в буфер целиком.")
    return 0


def _positive_int(raw: str) -> None:
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"нужно целое число больше нуля, а не {raw!r}")


def _copy_to_clipboard(text: str) -> None:
    """Буфер обмена — единственный способ унести марку с настоящим GS."""
    tool = shutil.which("pbcopy") or shutil.which("wl-copy") or shutil.which("xclip")
    if not tool:
        print("  ✗ не нашёл pbcopy / wl-copy / xclip — буфер обмена недоступен, "
              "возьмите строку JSON")
        return
    cmd = [tool, "-selection", "clipboard"] if tool.endswith("xclip") else [tool]
    subprocess.run(cmd, input=text.encode("utf-8"), check=True)
    print("\n✓ марки в буфере обмена — вместе с настоящим разделителем GS")


def run_assign_only(cfg: Settings, args: argparse.Namespace) -> int:
    """Режим «только назначение»: выдать заказ сборщику и курьеру в TMS."""
    order_id = _ask_order_id(args)
    print(f"\n── Назначение заказа {order_id}")
    assemble_order(cfg, order_id, args)
    return 0


def ensure_assigned(cfg: Settings, picker: PickingClient, order_id: str,
                    args: argparse.Namespace) -> None:
    """Назначает заказ, если он ещё не выдан настоящим сборщику и курьеру.

    Кто назначен, видно в самом заказе: `pickerStaffInfo` и `courierStaffInfo`.
    У неназначенного они `null`, у назначенного на заглушку — «Dummy». Гонять
    TMS по уже назначенному заказу незачем: это четыре запроса и переоткрытие
    формы ради того же результата.
    """
    if args.assemble is False:
        print("  пропущено (--no-assemble)")
        return

    if args.assemble is None:
        try:
            order = picker.order_get(order_id)
        except PickerOrderNotFound:
            print(f"  picker-service не нашёл заказ {order_id} — ищу в TMS ({cfg.tms_url})")
        else:
            issues = assignment_issues(order)
            if not issues:
                who = ", ".join(f"{role} {name}"
                                for role, name in assigned_performers(order).items())
                print(f"  уже назначен ({who}) — назначение пропускается")
                return
            print(f"  не назначен: {', '.join(issues)}")

    assemble_order(cfg, order_id, args, picker)


def run_picking_only(cfg: Settings, args: argparse.Namespace) -> int:
    """Режим «только сборка»: заказ уже создан, его нужно довести до конца."""
    order_id = _ask_order_id(args)

    picker = login_picker(cfg)
    try:
        print(f"\n── Назначение заказа {order_id}")
        ensure_assigned(cfg, picker, order_id, args)

        print(f"\n── Сборка заказа {order_id}")
        pick_and_deliver(cfg, order_id, args, picker=picker)
    finally:
        picker.close()
    return 0


# --- основной сценарий ------------------------------------------------------
#: Признаки собственных нужд: ключ, как их называть, флаг командной строки
OWN_NEEDS_KINDS = (("vet", "ветеринарные товары", "vet_own_needs"),
                   ("mark", "маркированные товары", "mark_own_needs"))


def choose_own_needs(api: LentaApi, args: argparse.Namespace) -> tuple[bool, bool]:
    """Собственные нужды заказа — перед переходом на чекаут.

    Переключать можно не всё: lookup отвечает, какой признак доступен
    (`abilityVetOwnNeeds` / `abilityMarkOwnNeeds`) и не заблокирован ли он
    (`*ReadOnly`). Недоступный признак не спрашивается и не отправляется —
    так же ведёт себя веб-морда, где галочка просто неактивна.
    """
    state = api.b2b.state_regulation()
    print(f"  собственные нужды: {state}")
    if state.message:
        print(f"  {state.message}")

    wanted = {}
    for kind, title, flag in OWN_NEEDS_KINDS:
        asked = getattr(args, flag)
        if not state.can_change(kind):
            if asked is not None:
                print(f"  {title}: признак недоступен для этой корзины, "
                      f"--{flag.replace('_', '-')} пропущен")
            wanted[kind] = state.enabled(kind)
            continue
        if asked is not None:
            wanted[kind] = asked
        elif _interactive() and not args.yes:
            # Умолчание — как стоит сейчас: Enter ничего не меняет
            current = state.enabled(kind)
            wanted[kind] = _choose(f"В собственные нужды — {title}?",
                                   ["Да", "Нет"], default=0 if current else 1) == 0
        else:
            wanted[kind] = state.enabled(kind)

    if wanted["vet"] == state.is_vet and wanted["mark"] == state.is_mark:
        # менять нечего — лишний proStateRegulationModify не нужен
        return wanted["vet"], wanted["mark"]

    api.b2b.set_state_regulation(vet=wanted["vet"], mark=wanted["mark"])
    return wanted["vet"], wanted["mark"]


def apply_reorder(args: argparse.Namespace, past: PastOrder) -> None:
    """Ответы из прошлого заказа — туда же, куда попадают флаги.

    Товары в корзину кладёт сам стенд (cartFromOrderAdd), поэтому `args.sku`
    здесь только для итога и для повтора последнего заказа. Миссия, адрес или
    магазин, оплата и собственные нужды — из заказа; явный флаг важнее.
    Интервал — сегодня, по возможности на то же время.
    """
    def fill(name: str, value) -> None:
        if getattr(args, name, None) in (None, "") and value not in (None, ""):
            setattr(args, name, value)

    fill("mission", "pickup" if past.is_pickup else "delivery")
    if past.is_pickup:
        fill("store_id", past.store_id)
    else:
        fill("address_id", past.address_id)
    fill("payment", str(past.payment_type_id) if past.payment_type_id is not None else None)
    fill("vet_own_needs", past.vet_own_needs)
    fill("mark_own_needs", past.mark_own_needs)
    args.sku = [f"{item.sku}:{item.units:g}" for item in past.items]
    if not args.interval_id:
        args.repeat_time = past.time_from[:5] or None
    args.today_only = True


def apply_repeat(args: argparse.Namespace, last: last_order.LastOrder) -> None:
    """Подставляет ответы прошлого заказа туда, куда попадают флаги.

    Флоу оформления от этого не меняется — он просто не задаёт вопросов,
    ответ на которые уже есть. Явно переданный флаг важнее сохранённого:
    `--repeat --sku 433217:5` повторит всё, кроме состава. Подтверждение
    «Создать заказ?» остаётся — повтор создаёт настоящий заказ.
    """
    def fill(name: str, value) -> None:
        if getattr(args, name, None) in (None, [], "") and value not in (None, ""):
            setattr(args, name, value)

    fill("phone", last.phone)
    fill("org", last.org_inn)
    fill("mission", last.mission)
    fill("address_kind", last.address_kind)
    fill("address_id", last.address_id)
    fill("store_id", last.store_id)
    fill("sku", [f"{sku}:{qty:g}" for sku, qty in last.items])
    fill("payment", last.payment)
    fill("vsd", last.vsd)
    fill("vet_own_needs", last.vet_own_needs)
    fill("mark_own_needs", last.mark_own_needs)
    if not args.interval_id:
        args.repeat_time = last.time_from
    print(f"── Повтор заказа {last.order_id or ''} от {last.saved_at.replace('T', ' ')}")
    print(f"  {last.summary()}; оплата {last.payment}"
          + (f"; интервал на {last.time_from}" if last.time_from else ""))


def choose_store(api: LentaApi, cfg: Settings, args: argparse.Namespace) -> int:
    """Магазин самовывоза: из списка доступных, а не вслепую из конфига.

    Раньше сюда подставлялся LO_PICKUP_STORE_ID, и ошибиться было нечем —
    заказ уезжал в магазин, которого в списке может не быть вовсе. Список
    приходит из stores/pickup/search, где id магазина и есть storeId заказа.
    """
    stores = api.store.pickup()
    if not stores:
        raise SystemExit(
            "Стенд не вернул ни одного магазина самовывоза. Выберите доставку "
            "или задайте магазин вручную: --store-id <id>"
        )

    # Умолчание — тот, что выбран на стенде, иначе из конфига, иначе первый
    default = next((i for i, s in enumerate(stores) if s.selected), None)
    if default is None:
        default = next((i for i, s in enumerate(stores)
                        if s.id == cfg.pickup_store_id), 0)

    if not _interactive() or args.yes or len(stores) == 1:
        chosen = stores[default]
        print(f"  магазин: {chosen}")
        return chosen.id

    return stores[_choose("Магазин самовывоза:", [str(s) for s in stores],
                          default=default)].id


def build_params(api: LentaApi, cfg: Settings, args: argparse.Namespace, *,
                 allow_back: bool = False) -> CheckoutParams:
    missions = [Mission.DELIVERY, Mission.PICKUP]
    if args.mission:
        mission = Mission(args.mission)
    else:
        # С -y вопросов не задаём: берётся первая миссия, как и везде в CLI
        mission = missions[_choose("Миссия заказа:", ["Доставка", "Самовывоз"],
                                   allow_back=allow_back,
                                   default=0) if not args.yes else 0]

    address_kind = None
    if mission is Mission.DELIVERY and args.address_id:
        pass  # адрес задан конкретно — его тип спрашивать незачем
    elif mission is Mission.DELIVERY:
        if args.address_kind:
            address_kind = AddressKind(args.address_kind)
        else:
            address_kind = list(AddressKind)[
                _choose("Тип адреса:", ["Обычный", "Меркурий", "ЕГАИС"],
                        allow_back=allow_back)
                if not args.yes else 0
            ]

    items = parse_sku(args.sku) if args.sku else None
    # Состав из .env: удобно, когда одна и та же корзина гоняется много раз
    if not items and cfg.order_items:
        items = parse_sku([cfg.order_items])
        print(f"  состав из LO_ORDER_ITEMS: {', '.join(f'{s}×{q:g}' for s, q in items)}")
    if not items:
        if cfg.skus and _interactive():
            labels = [f"{s.kind}: {s}" for s in cfg.skus] + ["ввести вручную"]
            idx = _choose("Товар:", labels, allow_back=allow_back)
            if idx < len(cfg.skus):
                qty = _ask(f"Количество для {cfg.skus[idx]}", "1")
                items = [(cfg.skus[idx].id, float(qty.replace(",", ".")))]
        if not items:
            items = parse_sku([_ask("SKU и количество (433217:2, 123456)")])

    vsd = args.vsd
    if address_kind is AddressKind.MERCURY and not vsd and _interactive():
        vsd = _ask("ВСД для Меркурий-адреса (Enter — пропустить)") or None

    store_id = args.store_id
    if mission is Mission.PICKUP and not store_id:
        store_id = choose_store(api, cfg, args)

    address_id = args.address_id
    if mission is Mission.DELIVERY and not address_id and _interactive() and not args.yes:
        candidates = api.address.of_kind(address_kind)
        if not candidates:
            raise SystemExit(f"Нет ни одного адреса типа {address_kind} у этого аккаунта.")
        if len(candidates) > 1:
            address_id = candidates[_choose("Адрес доставки:",
                                            [str(a) for a in candidates],
                                            allow_back=allow_back)].id

    return CheckoutParams(
        mission=mission,
        items=items,
        address_kind=address_kind,
        address_id=address_id,
        store_id=store_id,
        payment_type_id=args.payment,
        interval_index=args.interval,
        address_vsd=vsd,
        partner_card=args.partner_card,
        vet_own_needs=args.vet_own_needs,
        mark_own_needs=args.mark_own_needs,
    )


@dataclass
class _Checkout:
    """Состояние оформления, общее для всех шагов.

    Шаги складывают сюда выбранное, а не возвращают друг другу: при возврате
    назад шаг переигрывается и просто перезаписывает свои поля.
    """

    phone: str = ""
    org: Organization | None = None
    past: PastOrder | None = None
    params: CheckoutParams | None = None
    address: Address | None = None
    cart: Cart | None = None
    own_vet: bool = False
    own_mark: bool = False
    payment_id: str = ""
    interval: Interval | None = None
    pending: str = ""
    order: Order | None = None


def _step_error_handler(args: argparse.Namespace):
    """Что делать, когда шаг упал.

    В диалоге решает человек: чаще всего отказ стенда лечится другим выбором
    на предыдущем шаге — другим интервалом, другим способом оплаты. Поэтому
    «вернуться назад» стоит первым пунктом.

    Без диалога (CI, --yes) ошибка пробрасывается сразу: переигрывать шаги
    там, где никто не смотрит, значит молча оформить не тот заказ.
    """

    def decide(exc: Exception, step: Step) -> str:
        print(f"\n✗ {exc}", file=sys.stderr)
        if not _interactive() or args.yes:
            return "abort"
        if getattr(exc, "unreachable", False):
            print("  Сервис недоступен целиком — переигрывать шаг бессмысленно.")
            return "abort"
        return ["back", "retry", "abort"][_choose(
            "Что делаем?",
            ["Вернуться к прошлому шагу и выбрать иначе",
             "Повторить этот шаг",
             "Прервать"],
        )]

    return decide


def run(args: argparse.Namespace) -> int:
    if args.stand == "?":
        known = available_stands()
        print("Стенды: " + (", ".join(known) if known else "ни одного файла в env/"))
        return 0
    cfg = load_settings(args.stand)
    if args.log_file:
        cfg.log_file = args.log_file
    if args.log_dir is not None:
        cfg.log_dir = args.log_dir
    setup_logging(args.verbose, cfg.log_file, cfg.log_dir)
    if args.clear_session:
        clear_cache(cfg.token_cache_path)
        print("Кэш сессии очищен.")
        return 0

    mode = args.mode
    last = last_order.load(cfg.last_order_path)
    if last and last.stand != cfg.stand:
        last = None  # адреса, магазины и учётки у стендов свои — повторять нечего
    if args.repeat:
        mode = "repeat"
    if mode is None:
        if _interactive() and not args.yes:
            modes = ["shift", "assign", "pick", "full", "mark"]
            labels = ["Вывести пикера и курьера на смену",
                      "Назначение заказа на сборщика и курьера",
                      "Сборка уже созданного заказа",
                      "Полный флоу: заказ → назначение → сборка",
                      "Сгенерировать марку для товара"]
            if last:
                modes.append("repeat")
                labels.append(f"Повторить прошлый заказ ({last.summary()})")
            modes.append("reorder")
            labels.append("Повторить заказ по номеру — те же товары, на сегодня")
            mode = modes[_choose("Что делаем?", labels)]
        else:
            # Без диалога режим прежний: --yes и запуск из CI оформляют заказ
            mode = "full"
    if args.reorder:
        mode = "reorder"
    if mode == "reorder":
        if not args.reorder:
            default = last.order_id if last else ""
            args.reorder = _ask("Номер заказа", default).strip()
            if not args.reorder:
                raise SystemExit("Номер заказа не введён — повторять нечего.")
        mode = "full"
    if mode == "repeat":
        if last is None:
            raise SystemExit(
                f"Повторять нечего: на стенде {cfg.stand} ещё не оформлялся заказ "
                f"этим скриптом (файл {cfg.last_order_path}). Оформите один как обычно."
            )
        apply_repeat(args, last)
        mode = "full"
    if mode in ("shift", "assign", "pick", "mark"):
        runner = {"shift": run_shift_only,
                  "assign": run_assign_only,
                  "pick": run_picking_only,
                  "mark": run_mark_only}[mode]
        try:
            return runner(cfg, args)
        except LoApiError as exc:
            print(f"\n✗ {exc}", file=sys.stderr)
            return 1

    api = LentaApi(Context(cfg=cfg))
    flow = CheckoutFlow(api)

    try:
        state = _Checkout()
        decide = _step_error_handler(args)

        def step_auth() -> None:
            state.phone = authenticate(api, cfg, args)

        def step_org() -> None:
            state.org = choose_organization(api, args, allow_back=True)
            print(f"✓ {state.org}")

            if args.reorder:
                print(f"\n── Повтор заказа {args.reorder}")
                state.past = api.order.lookup(args.reorder)
                print(f"  {state.past.summary()} ({state.past.status_text})")
                for item in state.past.items:
                    print(f"  • {item}")
                apply_reorder(args, state.past)

        def step_mission() -> None:
            state.params = build_params(api, cfg, args, allow_back=True)
            state.address = flow.set_mission(state.params)
            where = (str(state.address) if state.address
                     else f"самовывоз, магазин {api.ctx.store_id}")
            print(f"✓ {where}")

        def step_cart() -> None:
            if args.clear_cart:
                api.cart.clear()
                print("  корзина очищена")
            if state.past is not None:
                cart, issues = flow.cart_from_order(state.past)
                if issues:
                    print("  ⚠ корзина отличается от заказа:")
                    for line in issues:
                        print(f"    {line}")
                    if not _interactive() or args.yes:
                        raise SystemExit(
                            "✗ Повтор без диалога остановлен: корзина не совпала с заказом, "
                            "а оформлять молча другой заказ нельзя. Проверьте наличие товаров."
                        )
            else:
                cart = flow.fill_cart(state.params.items)
            state.cart = cart
            print(f"✓ позиции: {cart.quantity_by_sku}, сумма: {cart.total}")
            state.own_vet, state.own_mark = choose_own_needs(api, args)

        def step_payment() -> None:
            print(f"  баланс организации: {api.b2b.balance()}")
            types, default_id, payment_id = flow.choose_payment(state.params.payment_type_id)
            if not args.payment and _interactive() and not args.yes:
                default_idx = next(
                    (i for i, p in enumerate(types) if str(p.id) == str(default_id)), 0
                )
                payment_id = str(types[_choose("Способ оплаты:", [str(p) for p in types],
                                               default_idx, allow_back=True)].id)
            state.payment_id = payment_id
            print(f"✓ выбрано: {payment_id}")

        def step_interval() -> None:
            # Интервалы перезапрашиваются каждый раз: их состав зависит от
            # способа оплаты, а тот мог измениться при возврате назад
            intervals = api.delivery.intervals(payment_type_id=state.payment_id)
            interval = choose_interval(intervals, args, allow_back=True)
            api.cart.set_interval(interval)
            state.interval = interval
            print(f"✓ {interval} (доступно {sum(1 for i in intervals if i.is_available)})")

        def step_confirm() -> None:
            final = api.cart.lookup()
            print(f"\nИтог: {final.quantity_by_sku}, сумма {final.total}, "
                  f"интервал {final.interval_id or state.interval.id}")

            if args.dry_run:
                print("\n--dry-run: заказ НЕ создавался. Корзина собрана и готова — "
                      "уберите --dry-run, чтобы оформить её заказом.")
                raise Stop

            # Создание — первым пунктом, по Enter: на тестовом стенде заказ дешёвый,
            # а оформление это основной сценарий, ради которого скрипт и запускают
            if args.yes or not _interactive():
                return
            picked = _choose("Создать заказ?", ["Да, создать", "Нет, отменить"],
                             allow_back=True)
            if picked == 1:
                print("Отменено, заказ не создавался.")
                raise Stop

        def step_create() -> None:
            pending, order = flow.place_order(state.params, state.payment_id)
            state.pending, state.order = pending, order
            print(f"\n✓ Заказ создан: {order.id}")
            print(f"  PendingRequestKey: {pending}")
            print(f"  Аккаунт: {state.phone}, организация: {state.org.name}")
            name_log_after_order(str(order.id))
            # Запоминаем только удачно созданный заказ: повторять отказ незачем
            last_order.save(cfg.last_order_path, last_order.LastOrder(
                stand=cfg.stand, phone=state.phone, org_inn=state.org.inn,
                org_name=state.org.name,
                mission=str(state.params.mission), items=list(state.params.items),
                payment=str(state.payment_id),
                address_kind=(str(state.params.address_kind)
                              if state.params.address_kind else None),
                address_id=state.address.id if state.address else None,
                address_title=str(state.address) if state.address else "",
                store_id=(api.ctx.store_id
                          if state.params.mission is Mission.PICKUP else None),
                vsd=state.params.address_vsd,
                vet_own_needs=state.own_vet, mark_own_needs=state.own_mark,
                time_from=state.interval.time_from, order_id=str(order.id),
            ))

        run_steps([
            # Авторизация необратима как цель возврата: сессия уже в кэше,
            # возвращаться в неё из выбора организации нечего
            Step("── Шаг 1: авторизация", step_auth, reversible=False),
            Step("\n── Шаг 2: организация", step_org),
            # Заголовок до вопросов: иначе выбор миссии выглядит частью шага 2
            Step("\n── Шаг 3: миссия", step_mission),
            Step("\n── Шаг 4: корзина", step_cart),
            Step("\n── Шаг 5: оплата", step_payment),
            Step("\n── Шаг 6: интервал", step_interval),
            Step("", step_confirm),
            Step("\n── Шаг 7: создание заказа", step_create, reversible=False),
        ], on_error=decide)

        # Остановились до создания: --dry-run или отказ от оформления
        if state.order is None:
            return 0

        if args.assemble or (args.assemble is None and _interactive() and not args.yes
                             and _choose("\nНужна сборка заказа?", ["Да", "Нет"]) == 0):
            print("\n── Шаг 8: назначение сборки")
            assemble_order(cfg, str(state.order.id), args)

            if args.pick or (args.pick is None and _interactive() and not args.yes
                             and _choose("\nСобрать заказ?", ["Да", "Нет"]) == 0):
                print("\n── Шаг 9: сборка")
                pick_and_deliver(cfg, str(state.order.id), args)
        return 0

    except LoApiError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    finally:
        api.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lo-order",
        description="Быстрое оформление заказа Лента B2B через API, минуя веб-морду.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  lo-order                                    всё спросит в диалоге\n"
            "  lo-order --sku 520787:1 --dry-run           собрать корзину, заказ не "
            "создавать\n"
            "  lo-order --mission pickup --sku 520787:1 -y  заказ самовывозом, без "
            "вопросов\n"
            "  lo-order --sku 520787:1 --assemble --pick\n"
            "                                              заказ → назначение → сборка "
            "→ доставка\n"
            "  lo-order --mode pick --order-id 900001126135\n"
            "                                              собрать уже созданный заказ\n"
            "  lo-order --mode shift                       вывести пикера и курьера "
            "на смену\n"
        ),
    )
    auth = p.add_argument_group("авторизация")
    auth.add_argument("--phone", help="номер телефона. Без него — выбор из LO_ACCOUNTS "
                                      "или ввод вручную; с -y — первый из списка")
    auth.add_argument("--code", help="код входа; без него будет запрошен в консоли")
    auth.add_argument("--token", help="готовый sessionToken — авторизация пропускается")
    auth.add_argument("--passport-id", help="passportId к готовому токену, если известен")
    auth.add_argument("--fresh", action="store_true", help="не использовать кэш сессии")
    auth.add_argument("--clear-session", action="store_true",
                      help="очистить кэш сессии и выйти")

    p.add_argument("--stand", metavar="ИМЯ",
                   help="стенд из папки env/ (test12, stage, …). По умолчанию — "
                        "LO_STAND из .env. «--stand ?» покажет список")
    p.add_argument("--mode", choices=["shift", "assign", "pick", "full", "mark"],
                   help="shift — только вывести сборщика и курьера на смену; "
                        "assign — назначить заказ на сборщика и курьера; "
                        "pick — собрать уже созданный заказ; "
                        "full — заказ, назначение и сборка; "
                        "mark — сгенерировать марку для товара. "
                        "Для assign и pick нужен --order-id")
    p.add_argument("--order-id", help="номер готового заказа для режимов assign и pick")
    p.add_argument("--reorder", metavar="НОМЕР",
                   help="оформить такой же заказ, как НОМЕР: те же товары (cartFromOrderAdd), "
                        "адрес или магазин, оплата и собственные нужды; интервал — сегодня. "
                        "Заказ должен принадлежать выбранному аккаунту")
    p.add_argument("--repeat", action="store_true",
                   help="повторить прошлый заказ этого стенда: те же аккаунт, организация, "
                        "адрес или магазин, состав и оплата; интервал — то же время в "
                        "ближайший день. Любой флаг рядом перекрывает сохранённое")

    mark = p.add_argument_group("генерация марок (--mode mark)")
    mark.add_argument("--item", metavar="SKU|ШК",
                      help="товар: SKU (штрихкод возьмётся из Goods) или сам штрихкод")
    mark.add_argument("--marking-kind", metavar="ГРУППА",
                      help="товарная группа марки: milk, chemistry, beer, … "
                           "По умолчанию — LO_MARKING_TYPES[sku], иначе LO_MARKING_TYPE")
    mark.add_argument("--count", type=int, help="сколько марок выпустить (по умолчанию 1)")
    mark.add_argument("--copy", action="store_true",
                      help="положить марки в буфер обмена — с настоящим разделителем GS")

    order = p.add_argument_group("заказ")
    order.add_argument("--org", help="организация: номер в списке, часть названия или ИНН")
    order.add_argument("--mission", choices=[str(m) for m in Mission])
    order.add_argument("--address-kind", choices=[str(k) for k in AddressKind])
    order.add_argument("--address-id", type=int,
                       help="конкретный адрес доставки; без него — первый нужного типа")
    own = order.add_mutually_exclusive_group()
    own.add_argument("--vet-own-needs", dest="vet_own_needs", action="store_true",
                     default=None, help="ветеринарные товары — в собственные нужды")
    own.add_argument("--no-vet-own-needs", dest="vet_own_needs", action="store_false",
                     help="ветеринарные товары — не в собственные нужды")
    own2 = order.add_mutually_exclusive_group()
    own2.add_argument("--mark-own-needs", dest="mark_own_needs", action="store_true",
                      default=None, help="маркированные товары — в собственные нужды")
    own2.add_argument("--no-mark-own-needs", dest="mark_own_needs", action="store_false",
                      help="маркированные товары — не в собственные нужды")

    order.add_argument("--store-id", type=int,
                       help="магазин самовывоза: Id из stores/pickup/search. "
                            "Без него список спрашивается при оформлении")
    order.add_argument("--sku", action="append", default=[],
                       help="SKU[:количество]; флаг повторяемый, внутри допустима запятая: "
                            "--sku 520787:2 --sku 433217 или --sku '520787:2, 433217'. "
                            "Без него берётся LO_ORDER_ITEMS из .env")
    order.add_argument("--payment", help="Id способа оплаты")
    order.add_argument("--interval", type=int, default=0,
                       help="индекс интервала среди доступных (по умолчанию первый)")
    order.add_argument("--interval-id",
                       help="конкретный интервал по Id; без него — выбор даты и времени")
    order.add_argument("--vsd", help="реальный ВСД для Меркурий-адреса")
    order.add_argument("--partner-card", help="карта лояльности (LO_PARTNER_DISCOUNT_CARD)")

    tms = p.add_argument_group("сборка (TMS Lite)")
    tms.add_argument("--assemble", dest="assemble", action="store_true", default=None,
                     help="назначить сборку сразу, не спрашивая")
    tms.add_argument("--no-assemble", dest="assemble", action="store_false",
                     help="не назначать сборку и не спрашивать о ней")
    tms.add_argument("--picker", help="сборщик: логин из LO_PICKERS, Id или часть имени. "
                                      "Выбирает и учётку для смены, и исполнителя в TMS")
    tms.add_argument("--courier", help="курьер: Id или часть имени")
    tms.add_argument("--open-shift", action="store_true",
                     help="вывести сборщика и курьера на смену, не проверяя списки TMS")

    pick = p.add_argument_group("сборка и доставка")
    pick.add_argument("--pick", dest="pick", action="store_true", default=None,
                      help="собрать заказ сразу после назначения, не спрашивая")
    pick.add_argument("--no-pick", dest="pick", action="store_false",
                      help="не собирать и не спрашивать о сборке")
    pick.add_argument("--deliver", dest="deliver", action="store_true", default=None,
                      help="провести доставку курьером до DELIVERED (это и так "
                           "делается по умолчанию)")
    pick.add_argument("--no-deliver", dest="deliver", action="store_false",
                      help="остановиться на собранном заказе, курьера не звать")
    pick.add_argument("--pick-mode", choices=["auto", "ask"],
                      help="auto — собрать всё автоматически; ask — по каждому товару "
                           "спросить, собирать или убирать, и ввести партии вручную. "
                           "Без флага спрашивается")
    pick.add_argument("--open-order", choices=["finish", "cancel"],
                      help="что делать с чужим незаконченным заказом, который мешает: "
                           "открытым у сборщика или несобранным в маршруте курьера. "
                           "finish — дособрать, cancel — отменить. Без флага спрашивается")
    pick.add_argument("--allow-partial", action="store_true",
                      help="собирать только поддерживаемые позиции, пропуская остальные")

    run_grp = p.add_argument_group("выполнение")
    run_grp.add_argument("--clear-cart", action="store_true", help="очистить корзину перед сборкой")
    run_grp.add_argument("--dry-run", action="store_true", help="остановиться перед orderAdd")
    run_grp.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    run_grp.add_argument("-v", "--verbose", action="store_true", help="подробный лог запросов")
    run_grp.add_argument("--log-file", metavar="PATH",
                         help="точный путь к файлу лога; перекрывает --log-dir")
    run_grp.add_argument("--log-dir", metavar="DIR",
                         help="папка логов; файл называется по номеру заказа "
                              "(LO_LOG_DIR, по умолчанию logs). Пустая строка — не писать")
    return p


_LOG_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%H:%M:%S"
)
# Файл текущего прогона: нужен, чтобы переименовать его по номеру заказа
_log_handler: logging.FileHandler | None = None
_log_path: Path | None = None
# Имя выбрано человеком через --log-file — переименовывать его нельзя
_log_name_fixed = False


# Флаги, значения которых в заголовок прогона не попадают: лог заказа читают
# и пересылают, а токен из него работает как пароль.
SECRET_FLAGS = ("--token", "--code", "--passport-id", "--partner-card")


def _safe_argv() -> str:
    """Командная строка прогона с замаскированными секретами."""
    parts, mask_next = [], False
    for arg in sys.argv:
        if mask_next:
            parts.append("***")
            mask_next = False
            continue
        flag = arg.split("=", 1)[0]
        if flag in SECRET_FLAGS:
            parts.append(f"{flag}=***" if "=" in arg else flag)
            mask_next = "=" not in arg
        else:
            parts.append(arg)
    return " ".join(parts)


def _run_banner() -> str:
    """Отбивка между прогонами: без неё два запуска сливаются в один поток."""
    return (f"\n{'=' * 78}\n"
            f"# прогон {datetime.now():%Y-%m-%d %H:%M:%S} | {_safe_argv()}\n"
            f"{'=' * 78}\n")


def setup_logging(verbose: bool, log_file: str, log_dir: str = "") -> None:
    """Консоль — сводка, файл — полные тела.

    Разделены намеренно: один ответ со списком интервалов в развёрнутом виде
    вытесняет из терминала весь остальной вывод, а в файле он читается и
    грепается. Поэтому логгеры пишут всё, а отсекает уже консольный обработчик.
    """
    global _log_handler, _log_path, _log_name_fixed

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    _log_handler = _log_path = None
    _log_name_fixed = bool(log_file)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_LOG_FORMAT)
    if not verbose:
        # Без -v консоль остаётся чистой: тела и сводка запросов идут в файл
        console.addFilter(lambda r: not r.name.startswith("lo_api.http"))
    root.addHandler(console)

    # Номер заказа в начале прогона неизвестен, поэтому файл заводится под
    # временным именем и переименовывается в name_log_after_order()
    if log_file:
        path = Path(log_file)
    elif log_dir:
        path = Path(log_dir) / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
    else:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_run_banner())
    _log_handler = logging.FileHandler(path, encoding="utf-8")
    _log_handler.setLevel(logging.DEBUG)
    _log_handler.setFormatter(_LOG_FORMAT)
    root.addHandler(_log_handler)
    _log_path = path
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def name_log_after_order(order_id: str) -> None:
    """Переименовывает лог прогона в «<номер заказа>.log».

    Вызывается, как только номер известен. Явно заданный --log-file не
    трогаем: там человек выбрал имя сам.
    """
    global _log_handler, _log_path
    if _log_handler is None or _log_path is None or _log_name_fixed:
        return

    target = _log_path.with_name(f"{order_id}.log")
    if target == _log_path:
        return

    root = logging.getLogger()
    _log_handler.close()
    root.removeHandler(_log_handler)
    if target.exists():
        # Тот же заказ собирают в несколько заходов: дособрать, довалидировать,
        # доставить. Каждый заход — свой прогон, и Path.replace затирал бы
        # историю предыдущих. Дописываем в конец, отбивка уже в начале файла
        with target.open("a", encoding="utf-8") as dst, \
             _log_path.open(encoding="utf-8") as src:
            shutil.copyfileobj(src, dst)
        _log_path.unlink()
    else:
        _log_path.replace(target)

    _log_handler = logging.FileHandler(target, encoding="utf-8")
    _log_handler.setLevel(logging.DEBUG)
    _log_handler.setFormatter(_LOG_FORMAT)
    root.addHandler(_log_handler)
    _log_path = target
    print(f"  лог заказа: {target}")


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
