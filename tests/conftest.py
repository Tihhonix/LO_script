"""Фикстуры: конфиг, аккаунт из пула, авторизованное API, чистая корзина.

Ключевое ограничение стенда: он лимитирует частоту запросов кода входа
(«Вы уже запрашивали код для входа», HTTP 500). Поэтому авторизация —
session-scoped: один логин на прогон (на каждого xdist-воркера), а не на тест.
"""

from __future__ import annotations

import logging

import pytest

from lo_api.clients import LentaApi
from lo_api.config import Account, Settings, Sku
from lo_api.constants import Mission, SkuKind
from lo_api.flows import CheckoutFlow
from lo_api.http.transport import Context
from lo_api.session_cache import CachedSession
from lo_api.session_cache import load as load_cache
from lo_api.session_cache import save as save_cache

log = logging.getLogger("tests")


def pytest_configure(config: pytest.Config) -> None:
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def cfg() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def worker_id(request: pytest.FixtureRequest) -> str:
    """worker_id без обязательной зависимости от pytest-xdist."""
    workerinput = getattr(request.config, "workerinput", None)
    return workerinput["workerid"] if workerinput else "master"


@pytest.fixture(scope="session")
def account(cfg: Settings, worker_id: str) -> Account:
    """Свой аккаунт каждому xdist-воркеру.

    Один аккаунт не может параллельно исполнять два сценария — корзина общая,
    и лимит на запрос кода тоже общий. Размер пула — жёсткий потолок
    параллелизма, поэтому проверяем явно.
    """
    if not cfg.accounts:
        pytest.skip("Пул аккаунтов пуст: задайте LO_ACCOUNTS (см. .env.example)")
    idx = 0 if worker_id == "master" else int(worker_id.removeprefix("gw"))
    assert idx < len(cfg.accounts), (
        f"Воркеров больше, чем аккаунтов в пуле ({len(cfg.accounts)}). "
        f"Уменьшите -n или расширьте LO_ACCOUNTS."
    )
    return cfg.accounts[idx]


# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def api(cfg: Settings):
    """Одно API на весь прогон: одна сессия, один логин, один DeviceId."""
    client = LentaApi(Context(cfg=cfg))
    yield client
    client.close()


@pytest.fixture(scope="session")
def authorized_api(api: LentaApi, account: Account, cfg: Settings) -> LentaApi:
    """Авторизованное API с выбранной организацией — один раз на прогон.

    Источник сессии тот же, что у lo-order, и в том же порядке:
    готовый токен → кэш прошлого запуска → вход по коду. Тесты не должны
    жечь лимит на код входа: обычно достаточно один раз выполнить
    `lo-order --dry-run`, и кэш подхватится здесь.
    """
    if cfg.session_token:
        api.auth.use_token(cfg.session_token, cfg.passport_id)
    else:
        cached = load_cache(cfg.token_cache_path, phone=account.phone,
                            max_age=cfg.token_max_age) if cfg.use_token_cache else None
        if cached:
            api.auth.use_token(cached.token, cached.passport_id)
            if cached.device_id:
                api.ctx.extra["device_id"] = cached.device_id
            if not api.auth.is_session_alive():
                api.ctx.session_token = None
                api.ctx.passport_id = None
        if not api.ctx.authorized:
            if not cfg.sms_stub_code:
                pytest.skip(
                    "Нет живой сессии и не задан код входа. Получите сессию один раз "
                    "(`lo-order --dry-run` — он сохранит её в кэш), либо задайте "
                    "LO_SESSION_TOKEN / LO_SMS_STUB_CODE в .env"
                )
            api.auth.login(account.phone, cfg.sms_stub_code)
            save_cache(cfg.token_cache_path, CachedSession(
                phone=account.phone,
                token=api.ctx.session_token or "",
                passport_id=api.ctx.passport_id or "",
                device_id=api.t.device_id,
            ))

    orgs = api.b2b.organizations()
    assert orgs, f"У аккаунта {account} нет доступных организаций"
    api.b2b.select(orgs[0])
    return api


@pytest.fixture
def fresh_api(cfg: Settings):
    """Неавторизованное API со своим контекстом — для негативных тестов.

    Отдельная фикстура, чтобы негативные проверки не портили session-scoped
    сессию, которой пользуются остальные тесты.
    """
    client = LentaApi(Context(cfg=cfg))
    yield client
    client.close()


@pytest.fixture(autouse=True)
def _reset_navigation_state(request: pytest.FixtureRequest):
    """Сбрасывает выбор адреса/магазина между тестами, сохраняя авторизацию.

    Сессия общая, поэтому состояние «куда доставляем» обязано быть чистым на
    входе в каждый тест — иначе тесты начинают зависеть от порядка запуска.
    """
    yield
    if "api" in request.fixturenames:
        try:
            ctx: Context = request.getfixturevalue("api").ctx
        except pytest.FixtureLookupError:
            return
        ctx.selected_address_id = None
        ctx.store_id = None
        ctx.mission = Mission.PICKUP


@pytest.fixture
def flow(authorized_api: LentaApi) -> CheckoutFlow:
    return CheckoutFlow(authorized_api)


@pytest.fixture
def clean_cart(authorized_api: LentaApi):
    """Пустая корзина до теста и после.

    Очистка ДО важнее, чем после: она защищает от мусора, оставленного упавшим
    предыдущим прогоном.
    """
    authorized_api.cart.clear()
    yield authorized_api.cart
    authorized_api.cart.clear()


# ---------------------------------------------------------------------------
@pytest.fixture
def sku(cfg: Settings):
    """Фабрика: sku(SkuKind.WEIGHT) → Sku, либо skip с внятной причиной."""

    def _get(kind: SkuKind) -> Sku:
        item = cfg.sku_of(kind)
        if item is None:
            pytest.skip(f"В LO_SKUS не настроен товар категории {kind}")
        return item

    return _get


@pytest.fixture
def pickup_ready(authorized_api: LentaApi, cfg: Settings) -> LentaApi:
    authorized_api.delivery.set_mode(Mission.PICKUP, store_id=cfg.pickup_store_id)
    return authorized_api
