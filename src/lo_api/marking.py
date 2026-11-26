"""Генераторы марок Datamatrix и штрихкодов — порт скриптов B2B-Pick.

Источники: `markingGenerator.js`, `eanGenerator.js` и функция `weightBarcode`
из `flow.js`. Форматы марок перенесены дословно: сервер их разбирает по длинам
полей, и «более правильная» марка ему не подойдёт.

Группа марки выбирается настройкой `LO_MARKING_TYPE`, а не по товару — так же
устроена и коллекция (переменная `marking_type`, по умолчанию `milk`).
"""

from __future__ import annotations

import random
import string

# Разделитель групп GS (U+001D) — настоящий управляющий символ.
#
# В коллекции написано `const gs = '\\u001D'`, то есть шесть печатных
# символов. Но Bruno подставляет марку ТЕКСТОМ в шаблон JSON-тела, где эта
# последовательность становится обычным JSON-escape, и парсер сервера
# превращает её в U+001D. Отправлять шесть символов буквально нельзя: стенд
# отвечает «Не удалось получить короткую марку для марки …».
GS = "\x1d"

ALPHABET = string.ascii_lowercase + string.digits + string.ascii_uppercase


def random_string(length: int) -> str:
    return "".join(random.choice(ALPHABET) for _ in range(length))


# --- штрихкоды --------------------------------------------------------------
def ean13_check_digit(first12: str) -> int:
    """Контрольная цифра EAN-13: нечётные позиции ×1, чётные ×3."""
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12[:12]))
    return (10 - total % 10) % 10


def is_valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    return ean13_check_digit(code[:12]) == int(code[12])


def gtin_check_digit(body: str) -> int:
    """Контрольная цифра GTIN любой длины: веса 3 и 1 справа налево."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def to_marking_barcode(code: str) -> str:
    """Штрихкод в тот вид, который ждут генераторы марок: 13 цифр.

    Марка начинается с `01` и GTIN-14, а генераторы подставляют `010` и
    13 цифр — то есть GTIN-13 с ведущим нулём. EAN-8 и UPC-A дополняются
    нулями слева, GTIN-14 с ведущим нулём его теряет. Контрольная цифра
    сверяется: марка с чужим штрихкодом стенду ни к чему.
    """
    digits = code.strip()
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        raise ValueError(
            f"{code!r} не похож на штрихкод: нужно 8, 12, 13 или 14 цифр "
            f"(EAN-8, UPC-A, EAN-13, GTIN-14)"
        )
    if gtin_check_digit(digits[:-1]) != int(digits[-1]):
        raise ValueError(f"у штрихкода {digits} не сходится контрольная цифра — "
                         f"проверьте, нет ли опечатки")
    if len(digits) == 14:
        if digits[0] != "0":
            raise ValueError(
                f"GTIN-14 {digits} начинается не с нуля: это код упаковки, а не "
                f"товара — генераторы марок его не поддерживают"
            )
        return digits[1:]
    return digits.rjust(13, "0")


def looks_like_barcode(code: str) -> bool:
    """Отличает штрихкод от SKU: SKU на стенде 6–7 цифр, штрихкод — от 8.

    Код от 9 цифр считается штрихкодом даже с неверной контрольной цифрой
    или длиной: иначе опечатка превращала бы его в SKU, и вместо «проверьте
    цифры» человек получал бы «товар не найден». Восемь цифр бывают и у SKU, и у
    EAN-8 — для них решает контрольная цифра.
    """
    digits = code.strip()
    if not digits.isdigit():
        return False
    if len(digits) >= 9:
        return True  # SKU такой длины не бывает — это штрихкод, пусть и с ошибкой
    return len(digits) == 8 and gtin_check_digit(digits[:-1]) == int(digits[-1])


def generate_ean13(full_ean13: str) -> str:
    """Новый EAN-13 на базе входного: цифры 10-12 случайные, сумма пересчитана.

    Уже корректный код возвращается как есть — так же поступает коллекция.
    """
    if len(full_ean13) != 13 or not full_ean13.isdigit():
        raise ValueError(f"EAN-13 должен содержать ровно 13 цифр, получено {full_ean13!r}")
    if is_valid_ean13(full_ean13):
        return full_ean13
    first12 = full_ean13[:9] + f"{random.randint(0, 999):03d}"
    return first12 + str(ean13_check_digit(first12))


def weight_barcode(base_barcode: str, grams: float) -> str:
    """Штрихкод весового товара: вес зашит в сам код.

    Формат из flow.js: первые 7 цифр базового кода, пять цифр веса в граммах
    и контрольная цифра. Округление half-up, как Math.round в JS.
    """
    prefix7 = str(base_barcode)[:7]
    first12 = prefix7 + f"{int(float(grams) + 0.5):05d}"[-5:]
    return first12 + str(ean13_check_digit(first12))


def weight_barcode_issues(barcode: str, grams: float) -> list[str]:
    """Что не так со штрихкодом весового товара.

    Проверка из коллекции: без неё сервер отвечает невнятным «товар не найден»,
    и причина — вес или контрольная цифра — остаётся неизвестной.
    """
    code = str(barcode or "")
    if len(code) != 13 or not code.isdigit():
        return [f"не 13 цифр: «{code}»"]

    issues = []
    expected = ean13_check_digit(code[:12])
    if int(code[12]) != expected:
        issues.append(f"контрольная цифра {code[12]}, ожидалась {expected}")
    in_barcode = int(code[7:12])
    want = int(float(grams) + 0.5)
    if in_barcode != want:
        issues.append(f"вес в ШК {in_barcode} г, в заказе {want} г")
    return issues


# --- марки ------------------------------------------------------------------
def _mark(barcode: str, head: str, tail: str) -> str:
    return f"010{barcode}{head}{GS}{tail}"


def _long(barcode: str, serial: str, extra: int = 44) -> str:
    return _mark(barcode, serial, f"91{random_string(4)}{GS}92{random_string(extra)}")


def _short(barcode: str, serial: str) -> str:
    return _mark(barcode, serial, f"93{random_string(4)}")


# Серийная часть у групп разная: «21» + N символов либо «215» + N.
# Длины взяты из markingGenerator.js дословно.
def _s21(n: int) -> str:
    return f"21{random_string(n)}"


def _s215(n: int) -> str:
    return f"215{random_string(n)}"


MARKING_GENERATORS = {
    # молочная продукция — умолчание коллекции
    "milk": lambda bc: _short(bc, _s21(6)),
    "shoes": lambda bc: _long(bc, _s21(13), 88),
    "lp": lambda bc: _long(bc, _s21(13)),
    "perfumery": lambda bc: _long(bc, _s21(13)),
    "tires": lambda bc: _long(bc, _s21(13)),
    "electronics": lambda bc: _long(bc, _s21(20)),
    "beer": lambda bc: _mark(bc, _s215(6), f"91{random_string(4)}"),
    "nabeer": lambda bc: _short(bc, _s215(6)),
    "softdrinks": lambda bc: _short(bc, _s215(12)),
    "bicycle": lambda bc: _long(bc, _s215(12)),
    "wheelchairs": lambda bc: _long(bc, _s215(12)),
    "vetpharma": lambda bc: _long(bc, _s215(12)),
    "bio_short": lambda bc: _short(bc, _s215(12)),
    "bio_long": lambda bc: _long(bc, _s215(12)),
    "antiseptic_short": lambda bc: _short(bc, _s215(12)),
    "antiseptic_long": lambda bc: _long(bc, _s215(12)),
    "autofluids_short": lambda bc: _short(bc, _s215(12)),
    "autofluids_long": lambda bc: _long(bc, _s215(12)),
    "cableraw_short": lambda bc: _short(bc, _s215(12)),
    "cableraw_long": lambda bc: _long(bc, _s215(12)),
    "grocery_short": lambda bc: _short(bc, _s215(12)),
    "grocery_long": lambda bc: _long(bc, _s215(12)),
    "opticfiber_short": lambda bc: _short(bc, _s215(12)),
    "opticfiber_long": lambda bc: _long(bc, _s215(12)),
    "radio_short": lambda bc: _short(bc, _s215(12)),
    "radio_long": lambda bc: _long(bc, _s215(12)),
    "vegetableoil_short": lambda bc: _short(bc, _s215(12)),
    "vegetableoil_long": lambda bc: _long(bc, _s215(12)),
    "toys_short": lambda bc: _short(bc, _s215(5)),
    "toys_long": lambda bc: _long(bc, _s215(12)),
    "chemistry_short": lambda bc: _short(bc, _s215(5)),
    "chemistry_long": lambda bc: _long(bc, _s215(5)),
    "conserve_short": lambda bc: _short(bc, _s215(5)),
    "conserve_long": lambda bc: _long(bc, _s215(5)),
    "heater_short": lambda bc: _short(bc, _s215(5)),
    "heater_long": lambda bc: _long(bc, _s215(5)),
    "petfood_short": lambda bc: _short(bc, _s215(5)),
    "petfood_long": lambda bc: _long(bc, _s215(5)),
    "seafood_short": lambda bc: _short(bc, _s215(5)),
    "seafood_long": lambda bc: _long(bc, _s215(5)),
    # Упакованная вода. В коллекции B2B-Pick генератора нет — формат по
    # Честному знаку: 21 + 13 символов серийника, крипто-хвост 93 + 4.
    # Совпадает с construction_short, но заведён отдельно, чтобы markType
    # WATER из Goods сопоставлялся сам и это было видно в журнале
    "water": lambda bc: _short(bc, _s21(13)),
    "construction_short": lambda bc: _short(bc, _s21(13)),
    "construction_long": lambda bc: _long(bc, _s21(13)),
    "sweets_short": lambda bc: _short(bc, _s21(6)),
    "sweets_long": lambda bc: _long(bc, _s21(13)),
    # табак: потребительская пачка идёт без префикса 010 и без разделителей
    "tobacco_consumer_package": lambda bc:
        f"{bc}{random_string(7)}{random_string(4)}{random_string(4)}",
    "tobacco_group_package": lambda bc:
        f"010{bc}21{random_string(7)}{GS}8005$100000{GS}93{random_string(4)}",
    "tobacco_set": lambda bc:
        f"010{bc}21{random_string(7)}{GS}8005$100000{GS}93{random_string(4)}",
}


#: markType из Goods, который не совпадает с именем группы генератора.
#: Остальные (MILK, SOFTDRINKS, CHEMISTRY, PETFOOD, SEAFOOD…) сопоставляются
#: resolve_marking_kind() по имени.
GOODS_MARK_TYPES = {
    "VEGETABOIL": "vegetableoil",   # так в Goods — без «LE»
}


def kind_from_goods(mark_type: str) -> str | None:
    """Группа генератора по markType из Goods; None — пусто или не узнали."""
    mark_type = (mark_type or "").strip()
    if not mark_type:
        return None
    try:
        return resolve_marking_kind(GOODS_MARK_TYPES.get(mark_type.upper(), mark_type))
    except ValueError:
        return None


def marking_groups() -> list[str]:
    """Группы для подсказки: без суффиксов длины, их подставит resolve_marking_kind."""
    return sorted({k.rsplit("_", 1)[0] if k.endswith(("_short", "_long")) else k
                   for k in MARKING_GENERATORS})


def resolve_marking_kind(kind: str) -> str:
    """Приводит название группы к имени генератора.

    Принимает короткое имя (`bio_short`), имя функции из коллекции
    (`markingBio_short`) и просто группу без суффикса (`SEAFOOD`). У групп с
    двумя вариантами длины суффикса нет только в последнем случае — тогда
    берётся короткая марка: она встречается чаще.
    """
    name = kind.strip()
    if name.lower().startswith("marking"):
        name = name[len("marking"):]
    name = name.strip("_ ").lower()

    if name in MARKING_GENERATORS:
        return name
    if f"{name}_short" in MARKING_GENERATORS:
        return f"{name}_short"

    similar = sorted(k for k in MARKING_GENERATORS if k.startswith(name[:4]))
    raise ValueError(
        f"нет генератора марки {kind!r}. "
        + (f"Похожие: {', '.join(similar)}. " if similar else "")
        + f"Всего групп {len(MARKING_GENERATORS)}, полный список — "
        f"в marking.MARKING_GENERATORS"
    )


def generate_marking(barcode: str, kind: str = "milk") -> str:
    """Марка Datamatrix для товарной группы."""
    return MARKING_GENERATORS[resolve_marking_kind(kind)](barcode)
