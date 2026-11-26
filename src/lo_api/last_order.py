"""Прошлый заказ — чтобы повторить его, не отвечая на вопросы заново.

Сохраняются ответы, а не сам заказ: аккаунт, организация, миссия, адрес или
магазин, состав, оплата, собственные нужды и время интервала. При повторе они
подставляются в те же места, куда попадают флаги командной строки, так что
флоу оформления проходит тот же — просто без вопросов.

Интервал хранится временем, а не Id: слот на вчерашнюю дату уже недоступен,
а «то же время в ближайший день» — именно то, что обычно хотят повторить.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("lo_api.last_order")


@dataclass
class LastOrder:
    stand: str
    phone: str
    org_inn: str
    org_name: str
    mission: str
    items: list[tuple[str, float]] = field(default_factory=list)
    payment: str = ""
    address_kind: str | None = None
    address_id: int | None = None
    address_title: str = ""
    store_id: int | None = None
    vsd: str | None = None
    vet_own_needs: bool | None = None
    mark_own_needs: bool | None = None
    time_from: str | None = None
    order_id: str = ""
    saved_at: str = ""

    def summary(self) -> str:
        """Одной строкой — для пункта меню."""
        where = (f"самовывоз, магазин {self.store_id}" if self.mission == "pickup"
                 else f"доставка, {self.address_title or f'адрес {self.address_id}'}")
        items = ", ".join(f"{sku}×{qty:g}" for sku, qty in self.items)
        return f"{where}; {items}; {self.org_name}"


def save(path: str | Path, order: LastOrder) -> None:
    order.saved_at = datetime.now().isoformat(timespec="seconds")
    Path(path).write_text(json.dumps(asdict(order), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info("Параметры заказа %s сохранены для повтора: %s", order.order_id, path)


def load(path: str | Path) -> LastOrder | None:
    """Прошлый заказ или None, если его нет или файл не читается.

    Нечитаемый файл — не повод падать: повтор лишь удобство, и прогон без
    него просто задаст вопросы как обычно.
    """
    file = Path(path)
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        data["items"] = [(str(sku), float(qty)) for sku, qty in data.get("items", [])]
        return LastOrder(**data)
    except (ValueError, TypeError) as exc:
        log.warning("Файл прошлого заказа %s не читается (%s) — повтор недоступен",
                    path, exc)
        return None
