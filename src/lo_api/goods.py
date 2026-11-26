"""Сервис Goods — штрихкоды товара.

Нужен только весовому товару: его штрихкод содержит граммовку, а базовый код
для неё берётся отсюда. Четвёртая система в проекте, с обычным REST и без
авторизации — ни `/jrpc`, ни конверта `Head/Body`.

Порт шага «GOODS barcodes» и функций `findBarcodes` / `pickWeightBarcode`
из коллекции B2B-Pick.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from lo_api.config import Settings
from lo_api.http.errors import LoApiError, network_error_text
from lo_api.http.transport import pretty_body

log = logging.getLogger("lo_api.goods")

# Типы весовых штрихкодов. Обычные (EAN) для весового товара не годятся:
# в весовой зашивается граммовка.
WEIGHT_BARCODE_TYPES = ("Z1", "Z2", "Z4", "Z7")


class GoodsError(LoApiError):
    """Ошибка обращения к сервису Goods."""


def find_barcodes(node: Any, depth: int = 0) -> list[dict] | None:
    """Ищет список штрихкодов в ответе любой вложенности.

    Порт findBarcodes: ответ Goods меняет форму от версии к версии, поэтому
    ищется не путь, а массив объектов с полями code и type.
    """
    if not isinstance(node, (dict, list)) or depth > 6:
        return None

    if isinstance(node, list):
        if node and all(isinstance(x, dict) and "code" in x and "type" in x for x in node):
            return node
        for child in node:
            found = find_barcodes(child, depth + 1)
            if found:
                return found
        return None

    # Сначала поле с ожидаемым именем, потом всё остальное
    if isinstance(node.get("barcodes"), list):
        found = find_barcodes(node["barcodes"], depth + 1)
        if found:
            return found
    for value in node.values():
        found = find_barcodes(value, depth + 1)
        if found:
            return found
    return None


def piece_barcodes(barcodes: list[dict]) -> list[str]:
    """Штучные штрихкоды товара — те, на которые наносится марка.

    Весовые (`Z1/Z2/Z4/Z7`) сюда не входят: в них зашивается граммовка, и
    марка на такой код не выпускается. Коробочные (`uom: KAR`, GTIN-14 вроде
    14600536001019) — тоже: марка идёт на единицу товара. Главный (`main`)
    штрихкод — первым.
    """
    found = []
    ordered = sorted(barcodes, key=lambda b: not b.get("main"))
    for b in ordered:
        code = str(b.get("code") or "").strip()
        if b.get("type") in WEIGHT_BARCODE_TYPES or not code.isdigit():
            continue
        if b.get("uom") not in (None, "", "ST"):
            continue
        if code not in found:
            found.append(code)
    return found


def find_card(node: Any, depth: int = 0, keys: tuple[str, ...] = ("markType", "isAlcohol")
              ) -> dict | None:
    """Карточка товара из ответа Goods — объект с полем markType.

    Лежит в `item.<sku без нулей>.item`, но путь ищется, а не зашивается:
    ответ Goods меняет форму от версии к версии.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        if any(k in node for k in keys):
            return node
        children = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = find_card(child, depth + 1, keys)
        if found is not None:
            return found
    return None


def pick_weight_barcode(barcodes: list[dict], marked: bool) -> str | None:
    """Базовый весовой штрихкод.

    Их бывает несколько, и различаются они флагом nonmark: маркированному
    товару нужен `nonmark: false`, немаркированному — `true`.
    """
    candidates = [b for b in barcodes if b.get("type") in WEIGHT_BARCODE_TYPES]
    if not candidates:
        return None
    if len(candidates) == 1:
        return str(candidates[0].get("code") or "")

    want = not marked
    match = [b for b in candidates if b.get("nonmark") is want]
    return str((match[0] if match else candidates[0]).get("code") or "")


class GoodsClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.base = f"https://{cfg.goods_url.strip('/')}"
        self.session = requests.Session()
        self.session.headers["user-agent"] = cfg.user_agent

    def item(self, store_id: int | None, sku: str) -> dict:
        path = f"/v2/gate/item/{self.cfg.goods_channel}/{store_id}/{sku}"
        try:
            resp = self.session.get(self.base + path, timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            raise GoodsError("goods/item", network_error_text(exc, self.cfg.goods_url)) from exc

        log.info("%-30s GET %s → %s", "goods/item", path, resp.status_code)
        if log.isEnabledFor(logging.DEBUG):
            limit = None if (self.cfg.log_file or self.cfg.log_dir) else self.cfg.log_body_limit
            log.debug("ответ goods/item:\n%s", pretty_body(resp.text, limit))
        if resp.status_code != 200:
            # Канал — единственная часть пути, которая берётся из конфига, и
            # именно на нём Goods спотыкается чаще всего: остальное приходит
            # из заказа. Без подсказки ошибка выглядит как поломка сервиса
            hint = ""
            if "channel" in resp.text:
                hint = (f"\nКанал «{self.cfg.goods_channel}» стенд не принял. "
                        f"Задайте LO_GOODS_CHANNEL=CHANNEL_B2B — это значение "
                        f"из корня коллекции B2B-Pick")
            raise GoodsError("goods/item",
                             f"тело: {resp.text[:300]}{hint}", status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise GoodsError("goods/item", f"ответ не JSON: {resp.text[:300]}") from exc

    def card(self, store_id: int | None, sku: str) -> dict:
        """Карточка товара: markType, isAlcohol, isWeight, штрихкоды.

        `markType` — товарная группа Честного знака («WATER», «MILK»…), по ней
        выбирается генератор марки. В orderGet её нет — только в Goods.
        """
        payload = self.item(store_id, sku)
        # Карточка без markType, но со штрихкодами тоже годится: группа марки
        # тогда возьмётся из настроек стенда
        card = find_card(payload) or find_card(payload, keys=("barcodes",))
        if card is None:
            keys = ", ".join(payload) if isinstance(payload, dict) else type(payload).__name__
            raise GoodsError("goods/item", f"в ответе Goods нет карточки товара {sku} "
                                           f"(поля markType). Верхние ключи: {keys}")
        return card

    def barcodes(self, store_id: int | None, sku: str) -> list[dict]:
        """Все штрихкоды товара в виде `[{code, type, …}]`."""
        payload = self.item(store_id, sku)
        found = find_barcodes(payload) or []
        if not found:
            raise GoodsError(
                "goods/item",
                f"Goods не вернул штрихкодов для sku {sku} в магазине {store_id}. "
                f"Проверьте номер товара или задайте штрихкод напрямую (--barcode). "
                f"Верхние ключи ответа: "
                f"{', '.join(payload) if isinstance(payload, dict) else type(payload).__name__}",
            )
        return found

    def weight_base_barcode(self, store_id: int | None, sku: str, *,
                            marked: bool = False) -> str:
        """Базовый штрихкод весового товара, из которого строится сканируемый."""
        payload = self.item(store_id, sku)
        barcodes = find_barcodes(payload) or []
        if not barcodes:
            raise GoodsError(
                "goods/item",
                f"Goods не вернул barcodes[] для sku {sku}. Верхние ключи ответа: "
                f"{', '.join(payload) if isinstance(payload, dict) else type(payload).__name__}",
            )

        base = pick_weight_barcode(barcodes, marked)
        if not base:
            seen = ", ".join(f"{b.get('type')}:{b.get('code')}" for b in barcodes)
            raise GoodsError(
                "goods/item",
                f"среди {len(barcodes)} ШК нет весового "
                f"({'/'.join(WEIGHT_BARCODE_TYPES)}) для sku {sku}. Что пришло: {seen}",
            )
        log.info("Весовой ШК %s: база %s (из %s кодов)", sku, base, len(barcodes))
        return base

    def close(self) -> None:
        self.session.close()
