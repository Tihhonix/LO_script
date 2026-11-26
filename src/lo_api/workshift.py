"""Вывод сборщика и курьера на смену.

Зачем: TMS назначает заказ только на сотрудников, выведенных на смену. Если
смена не открыта, в списках исполнителей остаются одни заглушки `Dummy`, и
назначить заказ не на кого.

Последовательности подтверждены коллекцией B2B-Pick (staffWorkshiftStart):
    сборщик: authTokenGet → tk → workshift_end → lunch → enable
    курьер:  authTokenGet → пустой вызов → tk → workshift_end → enable

Транспорт общий с `picking.py` — см. `picker.py`.
"""

from __future__ import annotations

import logging

from lo_api.picker import PickerError, PickerRpc

log = logging.getLogger("lo_api.workshift")

# Оставлено для обратной совместимости импортов: ошибка та же.
WorkshiftError = PickerError


class WorkshiftClient(PickerRpc):
    def _apply(self, key: str | None, value: str | None) -> None:
        params = {} if key is None else {"key": key, "value": value}
        self.call("staffWorkshiftStart_v2", params)

    def start_picker(self) -> str:
        """Выводит сборщика на смену. Возвращает его логин — он же Id в TMS."""
        cfg = self.cfg
        self.require_credentials(cfg.picker_login, cfg.picker_password, "LO_PICKER")
        self.login(cfg.picker_login, cfg.picker_password)
        self._apply("tk", cfg.workshift_tk)
        self._apply("workshift_end", cfg.workshift_end)
        self._apply("lunch", cfg.workshift_lunch)
        self._apply("enable", "true")
        log.info("Сборщик %s выведен на смену", cfg.picker_login)
        return cfg.picker_login

    def start_courier(self) -> str:
        """Выводит курьера на смену. Обеда в его последовательности нет."""
        cfg = self.cfg
        self.require_credentials(cfg.courier_login, cfg.courier_password, "LO_COURIER")
        self.login(cfg.courier_login, cfg.courier_password)
        # Пустой вызов первым — так делает коллекция: он открывает смену,
        # и без него последующие key/value отвергаются
        self._apply(None, None)
        self._apply("tk", cfg.workshift_tk)
        self._apply("workshift_end", cfg.workshift_end)
        self._apply("enable", "true")
        log.info("Курьер %s выведен на смену", cfg.courier_login)
        return cfg.courier_login
