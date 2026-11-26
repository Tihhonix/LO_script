"""Конфигурация. Ни одного секрета и ни одного идентификатора в коде.

Всё читается из окружения (префикс LO_) или из .env. См. .env.example.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from lo_api.constants import Mission, SkuKind

#: Папка с настройками стендов: по файлу на стенд, как environments в Bruno.
ENV_DIR = "env"

#: Сервисы, имя хоста которых выводится из названия стенда. Проверено по десяти
#: окружениям коллекции: везде `<сервис>-<стенд>.dev.lenta.tech`.
#: Goods сюда не входит — он один на все стенды (goods-app-stage).
STAND_SERVICES = {
    "base_url": "lenta-angular",
    "picker_proxy_url": "picker-service-proxy",
    "tms_url": "tms-lite-api",
}

#: Кому нужна схема в значении. Историческое различие: picker_proxy_url
#: подставляется в f"https://{...}/jrpc", а base_url и tms_url хранят её сами.
#: В файлах стендов хосты пишутся одинаково — голыми именами, как в коллекции,
#: а схему расставляет _derive_hosts().
SCHEMED_HOSTS = {"base_url", "tms_url"}

#: Все хосты стенда: выводимые из названия плюс общий для всех Goods.
HOST_FIELDS = (*STAND_SERVICES, "goods_url")


class Account(BaseModel):
    """Тестовый аккаунт из пула."""

    phone: str
    label: str = ""

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        """Приводит любой формат к 11 цифрам с кодом страны: 7XXXXXXXXXX.

        Подтверждено HAR-записью: браузер шлёт {"phone":"7XXXXXXXXXX"}. Раньше
        здесь обрезалось до 10 цифр — стенд отвечал на такой номер
        400 BAD_REQUEST, и это выглядело как неверный код.
        """
        digits = re.sub(r"\D", "", v)
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits
        if len(digits) != 11 or digits[0] != "7":
            raise ValueError(
                f"Номер {v!r} не приводится к формату 7XXXXXXXXXX (получилось {digits!r})"
            )
        return digits

    @property
    def national(self) -> str:
        """10 цифр без кода страны — если где-то понадобится."""
        return self.phone[1:]

    def __str__(self) -> str:
        return f"{self.label or 'acc'}:{self.phone}"


class Staff(BaseModel):
    """Сотрудник склада: сборщик или курьер.

    Логин здесь — это же его Id в списках TMS, поэтому по нему заказ и
    назначается. Пароль нужен для входа в picker-service.
    """

    login: str
    password: str
    label: str = ""

    @field_validator("login", "password")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def __str__(self) -> str:
        return f"{self.label} ({self.login})" if self.label else self.login


class Sku(BaseModel):
    """Тестовый товар."""

    id: str
    kind: SkuKind = SkuKind.REGULAR
    title: str = ""

    @field_validator("id")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError(f"SKU {v!r} должен состоять только из цифр")
        return v

    def __str__(self) -> str:
        return f"{self.id}({self.title or self.kind})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- стенд -------------------------------------------------------------
    # Стенд задаётся одним именем, хосты выводятся из него по шаблону.
    # Заданный явно хост важнее выведенного: на Mntk сервисы взяты со stage,
    # а tms — с test07, и шаблоном такое не описать.
    stand: str = "test12"
    host_template: str = "{service}-{stand}.dev.lenta.tech"

    base_url: str = ""

    # Организации живут на основном хосте, за api-gateway (подтверждено HAR).
    # Отдельный b2b-info-front-svc с префиксом /front — путь старого скрипта.
    b2b_org_path: str = "/api-gateway/v1/b2b/legalEntity/person"
    b2b_org_select_path: str = "/api-gateway/v1/users/profile/select"
    # Реквизиты выбранной организации, включая карту лояльности
    b2b_org_details_path: str = "/api-gateway/v1/b2b/legalEntity"
    # Магазины самовывоза: POST с пустым телом, только по sessiontoken
    stores_pickup_path: str = "/api-gateway/v1/stores/pickup/search"

    # Домен на тестовом окружении непринципиален (подтверждено), но должен быть
    # одинаковым во всех запросах — поэтому одна константа, а не 21 литерал.
    domain: str = "moscow"

    # --- пути авторизации --------------------------------------------------
    # Подтверждено HAR-записью: оба живут под /api-gateway/v1/..., префикса
    # /pro нет (он отдаёт 404). Запасной путь оставлен на случай отката стенда.
    # Гостевая сессия. Браузер всегда зовёт send/verify с уже существующим
    # sessiontoken, и verify возвращает ТОТ ЖЕ токен, повысив его до
    # авторизованного. Без сессии коду не к чему привязаться -> 400.
    auth_guest_path: str = "/api-gateway/v1/auth/passport/token/guest"
    auth_use_guest_session: bool = True

    auth_send_path: str = "/api-gateway/v1/auth/code/send"
    auth_verify_path: str = "/api-gateway/v1/auth/code/verify"
    auth_verify_fallback_path: str = "/pro/api-gateway/v1/auth/code/verify"
    auth_verify_autofallback: bool = True

    retail_brand: str = "lo"
    platform: str = "omniweb"
    device_os: str = "Web"
    device_os_version: str = "12.4.8"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    # --- ключи миссий ------------------------------------------------------
    mpk_delivery: str = "mp200-e86823887bcb4bf397df8c224f1a9dbf"
    mpk_pickup: str = "mp290-be269503c7334818b474f565fc266555"

    # --- данные стенда -----------------------------------------------------
    pickup_store_id: int = 3429  # ТК12
    partner_discount_card: str = ""

    # Код входа. Пусто = спросить у пользователя (или взять из --code).
    # Раньше здесь было "1234" по умолчанию — стенд отвечает на него
    # 400 BAD_REQUEST, и это выглядело как поломка кода, а не как неверный код.
    sms_stub_code: str = ""

    # --- сессия ------------------------------------------------------------
    # Готовый токен: полностью минует авторизацию. Снимается из DevTools
    # (кука Utk_SssTkn) или из предыдущего успешного входа.
    session_token: str = ""
    passport_id: str = ""

    # Кэш сессии между запусками — главное лекарство от лимита на код входа.
    token_cache_path: str = ".lo_session.json"
    # Ответы последнего оформленного заказа — для пункта «Повторить прошлый»
    last_order_path: str = ".lo_last_order.json"
    token_max_age: float = 6 * 3600  # 6 часов
    use_token_cache: bool = True

    # --- пул аккаунтов и товаров ------------------------------------------
    accounts: list[Account] = Field(default_factory=list)
    skus: list[Sku] = Field(default_factory=list)

    # Состав заказа по умолчанию — «SKU:количество» через запятую.
    # Отличается от skus: там каталог товаров по категориям для тестов,
    # здесь — конкретная корзина, которую собирает lo-order без --sku.
    order_items: str = ""

    # --- TMS Lite: назначение сборщика и курьера ---------------------------
    # Отдельная система с Django-админкой: вход по логину и паролю, не по коду.
    tms_url: str = ""  # пусто — вывести из stand
    tms_user: str = ""
    tms_password: str = ""
    # Режимы переноса из формы расследования (подтверждены HAR):
    #   picker:  hand | instant | «» (не переносить)
    #   courier: auto | hand | auto_taxi | hand_taxi | «»
    #   taxi:    auction_now | auction_due | yandex_now | yandex_due
    tms_picker_mode: str = "hand"
    tms_courier_mode: str = "hand"
    tms_taxi_mode: str = "auction_now"
    tms_delivery_type: str = "REGULAR"
    # TMS отвечает 200 и тогда, когда назначение не применилось. Поэтому после
    # отправки формы заказ перечитывается: столько раз, с такой паузой, а при
    # неудаче форма отправляется заново — до tms_assign_attempts раз
    tms_assign_checks: int = 4
    tms_assign_check_delay: float = 3.0
    tms_assign_attempts: int = 2

    # --- вывод на смену (picker-service-proxy) -----------------------------
    # TMS назначает заказ только на сотрудников, открывших смену. Если её нет,
    # в списках исполнителей остаются одни Dummy и назначать некого.
    picker_proxy_url: str = ""  # пусто — вывести из stand
    picker_client: str = "androin_11_900"
    picker_device_id: str = "8888"
    mp_picker_id: int = 50
    mp_picker_key: str = ""

    # Логин сотрудника здесь — это же его Id в списках TMS.
    # Списки нужны, чтобы гонять тесты под разными учётками: какая пойдёт в
    # дело, выбирается при выводе на смену. Одиночные переменные ниже —
    # прежний способ задать одного сотрудника, они остались рабочими.
    pickers: list[Staff] = Field(default_factory=list)
    couriers: list[Staff] = Field(default_factory=list)

    picker_login: str = ""
    picker_password: str = ""
    courier_login: str = ""
    courier_password: str = ""

    # Параметры смены из коллекции B2B-Pick
    workshift_tk: str = "43"
    workshift_end: str = "23:00"
    workshift_lunch: str = "20:30-21:00"

    # --- сборка непростых позиций ------------------------------------------
    # Товарная группа марки Datamatrix (переменная marking_type в коллекции).
    # Список групп — в marking.MARKING_GENERATORS.
    marking_type: str = "milk"
    # Группа для отдельных товаров: у воды, икры и сгущёнки форматы разные,
    # и общая группа на весь заказ даёт «не удалось получить короткую марку».
    marking_types: dict[str, str] = Field(default_factory=dict)
    # Производитель для партионных позиций; если такого нет в ответе
    # sapManufacturer, берётся первый доступный.
    preferred_manufacturer: str = "ПП для Ленты"
    # Дата партии ГГГГ-ММ-ДД. Приоритет сверху вниз, как в batchDate из flow.js:
    #   1. batch_dates[sku]                — своя каждому товару
    #   2. batch_date_add / batch_date_sap — всей ветке (sapBatchAdd / sapBatch)
    #   3. batches[] из заказа
    #   4. batch_date                      — всем партионным сразу
    batch_dates: dict[str, str] = Field(default_factory=dict)
    batch_date_add: str = ""  # ветка sapBatchAdd — MARK_PEU_BATCH
    batch_date_sap: str = ""  # ветка sapBatch — BATCH и MARK_OSU_BATCH
    batch_date: str = ""

    # Валидация в SAP асинхронная: sapValidate повторяется после каждого
    # неуспешного чтения статуса, пока не придёт SAP_VALIDATING_SUCCESS.
    sap_validate_attempts: int = 20
    sap_validate_delay: float = 15.0
    # Сколько раз исправлять заказ после SAP_VALIDATING_ERROR и отправлять
    # повторно, прежде чем остановиться: дальше проблема уже не в марках
    sap_error_retries: int = 2

    # Сервис Goods: у весового товара штрихкод содержит граммовку, а базовый
    # код для неё берётся отсюда. Канал задан в корне коллекции (переменная
    # channel в opencollection.yml), а не в окружениях — отсюда прежняя догадка
    # «lo», на которую стенд отвечает «lo is not valid».
    goods_url: str = "goods-app-stage.dev.lenta.tech"
    goods_channel: str = "CHANNEL_B2B"
    # Магазин в пути Goods — номер ТК, а не storeId самовывоза: в заказах
    # test12 склад приходит со storeId 12, и Goods спрашивается по нему.
    # Нужен генератору марок, чтобы найти штрихкод по SKU
    goods_store_id: int | None = None

    # Заказ в собственные нужды: партии у партионных товаров не запрашиваются.
    # По умолчанию определяется по ответам стенда (markOwnNeeds и vetOwnNeeds
    # разом); значение здесь перекрывает то, что сказал стенд.
    own_needs: bool | None = None

    @field_validator("own_needs", mode="before")
    @classmethod
    def _empty_means_auto(cls, v: object) -> object:
        """Пустая строка в .env значит «определять по ответам стенда»."""
        return None if isinstance(v, str) and not v.strip() else v

    # Стенд регулярно отдаёт 500 «error proxy» на шагах смены. Это не отказ
    # бизнес-логики, а нестабильность связки сервисов, поэтому попыток больше,
    # чем у обычных запросов: шаги смены идемпотентны, повтор безопасен.
    workshift_retry_attempts: int = 5

    # --- поведение клиента -------------------------------------------------
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    retry_attempts: int = 3
    retry_backoff: float = 0.5
    order_wait_timeout: float = 60.0
    order_poll_interval: float = 2.0
    log_bodies: bool = True
    log_body_limit: int = 2000
    # Папка для логов: на каждый прогон свой файл, который переименовывается
    # в «<номер заказа>.log», как только номер становится известен.
    log_dir: str = "logs"
    # Точный путь к файлу лога. Задан — перекрывает log_dir и переименование.
    log_file: str = ""

    # ----------------------------------------------------------------------
    @model_validator(mode="after")
    def _sync_staff(self) -> Settings:
        """Держит список сотрудников и одиночные переменные в согласии.

        Весь остальной код работает с `picker_login`/`picker_password` — это
        «текущий» сотрудник, которого при выводе на смену можно переключить.
        Список задаёт, из кого выбирать; если задана только одна учётка старым
        способом, она и становится списком из одного.
        """
        for role in ("picker", "courier"):
            staff = getattr(self, f"{role}s")
            login = getattr(self, f"{role}_login")
            password = getattr(self, f"{role}_password")
            if staff and not login:
                setattr(self, f"{role}_login", staff[0].login)
                setattr(self, f"{role}_password", staff[0].password)
            elif login and not any(s.login == login for s in staff):
                staff.insert(0, Staff(login=login, password=password))
        return self

    def use_staff(self, role: str, staff: Staff) -> None:
        """Делает сотрудника текущим: под ним пойдут смена, сборка и доставка."""
        setattr(self, f"{role}_login", staff.login)
        setattr(self, f"{role}_password", staff.password)

    @model_validator(mode="after")
    def _derive_hosts(self) -> Settings:
        """Достраивает незаданные хосты из названия стенда.

        Пустое значение означает «взять со стенда», а не «хоста нет»: так
        переключение между test12, stage и operation остаётся одной
        переменной, но любой хост можно прибить гвоздями по отдельности.
        """
        for field, service in STAND_SERVICES.items():
            if getattr(self, field):
                continue
            setattr(self, field, self.host_template.format(service=service, stand=self.stand))

        # Схему расставляем сами: в файле стенда хосты пишутся одинаково,
        # голыми именами, а кому она нужна в значении — знает SCHEMED_HOSTS
        for field in HOST_FIELDS:
            host = str(getattr(self, field) or "").strip().rstrip("/")
            bare = host.split("//", 1)[-1]
            setattr(self, field, f"https://{bare}" if field in SCHEMED_HOSTS else bare)
        return self

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def mpk(self, mission: Mission) -> str:
        return self.mpk_delivery if mission is Mission.DELIVERY else self.mpk_pickup

    def sku_of(self, kind: SkuKind) -> Sku | None:
        """Первый настроенный товар нужной категории, либо None.

        None — законный ответ: он означает «в конфиге нет такого товара»,
        и тест по этой категории должен быть пропущен, а не упасть.
        """
        return next((s for s in self.skus if s.kind is kind), None)

    def skus_of(self, kind: SkuKind) -> list[Sku]:
        return [s for s in self.skus if s.kind is kind]


def _explain_bad_value(exc: SettingsError, files: list[str]) -> str:
    """Где именно сломано значение, которое pydantic не смог разобрать.

    Сам pydantic говорит только «error parsing value for field accounts» — ни
    файла, ни места. JSON-списки вроде LO_ACCOUNTS пишутся руками в одну
    строку, и потерянная скобка в них так выглядела бы как «не принимает
    пробелы». Здесь значение разбирается заново, и показывается точка поломки
    со скелетом строки, где содержимое строк скрыто.
    """
    field = re.search(r'field "(\w+)"', str(exc))
    if not field:
        return str(exc)
    name = f"LO_{field.group(1).upper()}"
    # Последний файл важнее: .env накладывается поверх файла стенда
    for path in reversed(files):
        raw = dotenv_values(path).get(name) if Path(path).is_file() else None
        if raw is None:
            continue
        try:
            json.loads(raw)
        except json.JSONDecodeError as err:
            skeleton = re.sub(r'"[^"]*"', '"…"', raw)
            return (
                f"{name} в {path} — не JSON: {err.msg}, символ {err.pos + 1}.\n"
                f"  около: …{raw[max(0, err.pos - 25):err.pos]}⟨здесь⟩"
                f"{raw[err.pos:err.pos + 15]}…\n"
                f"  скелет: {skeleton}\n"
                f"Частая причина — потерянная фигурная скобка у одного из элементов "
                f"списка: каждый должен быть в {{…}}, между ними запятая."
            )
        return f"{name} в {path} не подходит по формату: {exc.__cause__ or exc}"
    return str(exc)


def available_stands(env_dir: str = ENV_DIR) -> list[str]:
    """Стенды, для которых есть файл настроек."""
    return sorted(p.stem for p in Path(env_dir).glob("*.env")) if Path(env_dir).is_dir() else []


def load_settings(stand: str | None = None, env_dir: str = ENV_DIR) -> Settings:
    """Конфигурация стенда: общий env/<стенд>.env, личный .env поверх него.

    Разделение то же, что у окружений Bruno: в файле стенда лежит общее —
    учётки сотрудников, магазин, товары; в .env остаётся личное — свой номер
    телефона, токен сессии, настройки логов. Личное важнее общего, поэтому
    .env накладывается последним.
    """
    try:
        return _load(stand, env_dir)
    except SettingsError as exc:
        files = [".env"]
        name = stand or os.getenv("LO_STAND")
        if name:
            files.insert(0, str(Path(env_dir) / f"{name}.env"))
        raise SystemExit(f"✗ Не читается конфигурация.\n{_explain_bad_value(exc, files)}") \
            from None


def _load(stand: str | None, env_dir: str) -> Settings:
    name = stand or os.getenv("LO_STAND") or Settings().stand
    path = Path(env_dir) / f"{name}.env"

    if not path.is_file():
        known = available_stands(env_dir)
        if known:
            raise SystemExit(
                f"Нет настроек стенда {name!r}: файла {path} не существует.\n"
                f"Есть: {', '.join(known)}. Заведите файл по образцу соседнего "
                f"или укажите другой стенд через --stand."
            )
        # Папки стендов нет вовсе — старый способ с одним .env продолжает работать
        return Settings(stand=name)

    return Settings(_env_file=[str(path), ".env"], stand=name)
