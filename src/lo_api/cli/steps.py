"""Шаги сценария с возможностью вернуться назад.

Оформление — цепочка шагов, на каждом из которых человек что-то выбирает.
Ошибиться легко: не тот адрес, не тот способ оплаты. Раньше единственным
выходом был Ctrl+C и запуск заново — с повторной авторизацией, пересборкой
корзины и новым выбором всего остального.

Поэтому шаги выполняются циклом, а не подряд: с любого можно вернуться к
предыдущему и переиграть выбор. Тем же механизмом обрабатывается отказ
стенда — вместо падения человек решает, повторить шаг, вернуться назад или
прервать.

Возврат безопасен, потому что каждый шаг пересчитывает своё состояние с нуля:
корзина задаёт количества абсолютным cartItemModify, интервалы
перезапрашиваются под выбранную оплату, адрес переустанавливается целиком.
Шаг, который так переигрывать нельзя (создание заказа), помечается
`reversible=False` — вернуться в него нельзя, только пройти заново с начала.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("lo_api.steps")

#: Пункт меню и слова, которыми просят вернуться назад
BACK_LABEL = "« Назад к прошлому шагу"
BACK_WORDS = frozenset({"<", "<<", "b", "back", "н", "назад"})


class Back(Exception):  # это управляющий сигнал, а не сбой
    """Человек попросил вернуться: шаг не выполнен, нужен предыдущий."""


class Stop(Exception):  # тоже сигнал: сценарий закончен штатно
    """Сценарий завершён досрочно и успешно (например, --dry-run)."""


@dataclass
class Step:
    """Один шаг сценария.

    `title` печатается перед выполнением — в том числе при повторном заходе,
    чтобы было видно, куда именно вернулись.

    `reversible` отвечает на вопрос «можно ли вернуться В этот шаг», а не
    «можно ли из него выйти». Необратимые шаги пропускаются при поиске цели
    возврата: вернуться к созданию заказа нельзя, оно уже случилось.
    """

    title: str
    run: Callable[[], None]
    reversible: bool = True


def previous_reversible(steps: list[Step], index: int) -> int | None:
    """Ближайший шаг до `index`, в который можно вернуться."""
    for i in range(index - 1, -1, -1):
        if steps[i].reversible:
            return i
    return None


def run_steps(steps: list[Step], *,
              on_error: Callable[[Exception, Step], str] | None = None,
              say: Callable[[str], None] = print) -> None:
    """Выполняет шаги, обрабатывая возврат назад и отказы стенда.

    `on_error` получает исключение и шаг, а возвращает решение:
    ``"retry"`` — повторить тот же шаг, ``"back"`` — вернуться к предыдущему,
    ``"abort"`` — пробросить исключение дальше. Если обработчик не задан
    (неинтерактивный запуск, CI), любая ошибка пробрасывается сразу: молча
    переигрывать шаги там, где никто не смотрит, нельзя.
    """
    index = 0
    while index < len(steps):
        step = steps[index]
        if step.title:
            say(step.title)
        try:
            step.run()
        except Stop:
            return
        except Back:
            target = previous_reversible(steps, index)
            if target is None:
                say("Это первый шаг — возвращаться некуда.")
                continue
            say(f"\n« Возврат: {steps[target].title.strip()}")
            index = target
            continue
        except Exception as exc:  # решение принимает обработчик
            if on_error is None:
                raise
            decision = on_error(exc, step)
            if decision == "retry":
                log.info("Повтор шага %r после ошибки", step.title.strip())
                continue
            if decision == "back":
                target = previous_reversible(steps, index)
                if target is None:
                    raise
                say(f"\n« Возврат: {steps[target].title.strip()}")
                index = target
                continue
            raise
        index += 1
