"""Интерактивный раннер — замена старого скрипта.

Тесты параметризованы и неинтерактивны, но ручной прогон «руками, с выбором»
остаётся полезным: посмотреть новый SKU, проверить конкретный адрес, снять лог.
Раннер использует те же клиенты, что и тесты, поэтому расхождения между
«как я проверял руками» и «как проверяет CI» невозможны.

    lo-flow              # или: python -m lo_api.cli.run_flow
"""

from __future__ import annotations

import logging
import sys

from lo_api.clients import LentaApi
from lo_api.config import Settings, load_settings
from lo_api.constants import AddressKind, Mission
from lo_api.flows import CheckoutFlow, CheckoutParams
from lo_api.http.errors import LoApiError
from lo_api.http.transport import Context


def _ask_choice(title: str, options: list[str], default: int | None = None) -> int:
    print(f"\n{title}")
    for i, opt in enumerate(options, start=1):
        mark = " (по умолчанию)" if default is not None and i - 1 == default else ""
        print(f"  {i}. {opt}{mark}")
    while True:
        raw = input("Номер: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("Введите номер из списка.")


def _ask_phone(cfg: Settings) -> str:
    from lo_api.config import Account

    if cfg.accounts:
        labels = [str(a) for a in cfg.accounts] + ["ввести другой номер"]
        idx = _ask_choice("Аккаунт:", labels, default=0)
        if idx < len(cfg.accounts):
            return cfg.accounts[idx].phone
    while True:
        raw = input("Номер телефона: ").strip()
        try:
            return Account(phone=raw).phone
        except ValueError as exc:
            print(f"Ошибка: {exc}")


def _ask_items(cfg: Settings) -> list[tuple[str, float]]:
    """SKU из конфига по категориям, с возможностью ввести свои."""
    presets = [f"{s.kind}: {s}" for s in cfg.skus]
    options = [*presets, "ввести SKU вручную"]
    if not presets:
        print("\nВ LO_SKUS ничего не настроено — вводим вручную.")
        return _ask_items_manual()

    idx = _ask_choice("Товар:", options, default=0)
    if idx < len(cfg.skus):
        chosen = cfg.skus[idx]
        qty = input(f"Количество для {chosen} [1]: ").strip() or "1"
        return [(chosen.id, float(qty))]
    return _ask_items_manual()


def _ask_items_manual() -> list[tuple[str, float]]:
    while True:
        raw = input("SKU и количество (например 433217:2, 123456): ").strip()
        items: list[tuple[str, float]] = []
        ok = True
        for part in (p.strip() for p in raw.split(",") if p.strip()):
            sku, _, qty = part.partition(":")
            sku, qty = sku.strip(), (qty.strip() or "1")
            if not sku.isdigit():
                print(f"Ошибка: SKU {sku!r} должен состоять из цифр.")
                ok = False
                break
            try:
                q = float(qty.replace(",", "."))
            except ValueError:
                print(f"Ошибка: количество {qty!r} не число.")
                ok = False
                break
            if q <= 0:
                print("Ошибка: количество должно быть больше нуля.")
                ok = False
                break
            items.append((sku, q))
        if ok and items:
            return items
        print("Попробуйте снова.\n")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_settings()
    api = LentaApi(Context(cfg=cfg))
    flow = CheckoutFlow(api)

    try:
        phone = _ask_phone(cfg)
        api.auth.login(phone)

        orgs = api.b2b.organizations()
        if not orgs:
            print("У аккаунта нет доступных организаций.")
            return 1
        org = orgs[_ask_choice("Организация:", [str(o) for o in orgs], default=0)]
        api.b2b.select(org)

        mission = [Mission.DELIVERY, Mission.PICKUP][
            _ask_choice("Миссия заказа:", ["Доставка", "Самовывоз"], default=0)
        ]

        address_kind = None
        if mission is Mission.DELIVERY:
            kinds = list(AddressKind)
            address_kind = kinds[
                _ask_choice("Тип адреса:", ["Обычный", "Меркурий", "ЕГАИС"], default=0)
            ]

        items = _ask_items(cfg)

        vsd = None
        if address_kind is AddressKind.MERCURY:
            vsd = input("ВСД для Меркурий-адреса (Enter — пропустить): ").strip() or None

        params = CheckoutParams(
            mission=mission, items=items,
            address_kind=address_kind, address_vsd=vsd,
        )

        # авторизация и выбор организации уже выполнены — идём по фазам
        flow.set_mission(params)
        flow.fill_cart(params.items)
        api.b2b.state_regulation()

        types, default_id, _ = flow.choose_payment(None)
        labels = [str(p) for p in types]
        default_idx = next(
            (i for i, p in enumerate(types) if str(p.id) == str(default_id)), 0
        )
        payment_id = str(types[_ask_choice("Способ оплаты:", labels, default=default_idx)].id)

        intervals, chosen = flow.choose_interval(0)
        print(f"\nВыбран интервал: {chosen} (доступно {len(intervals)})")

        cart = api.cart.lookup()
        print(f"\nКорзина: {cart.quantity_by_sku}, сумма: {cart.total}")

        if input("\nСоздать заказ? [y/N]: ").strip().lower() not in ("y", "д", "yes", "да"):
            print("Заказ не создавался.")
            return 0

        pending, order = flow.place_order(params, payment_id)
        print(f"\n✓ Заказ создан: Id={order.id} (PendingRequestKey={pending})")
        return 0

    except LoApiError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.")
        return 130
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
