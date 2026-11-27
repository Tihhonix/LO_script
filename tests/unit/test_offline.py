"""Оффлайн-тесты: проверяют логику каркаса без обращения к стенду.

Прогоняются везде и всегда, включая CI без доступа к тестовому контуру.
Каждый тест здесь закрывает конкретный баг, найденный в старом скрипте.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import pytest
import requests

from lo_api.clients import LentaApi, PaymentClient
from lo_api.config import Account, Settings, Sku, Staff
from lo_api.constants import CLIENT_VERSION, AddressKind, Mission, SkuKind
from lo_api.http.errors import BusinessError, HttpStatusError, LoApiError, NotJsonError
from lo_api.http.transport import Context, Transport
from lo_api.models import Address, Cart, Interval, PaymentType, Store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_goods_network(request, monkeypatch):
    """Сборка в тестах не ходит в Goods за карточкой товара.

    Карточка нужна для группы марки и признака алкоголя, но настоящий Goods
    из оффлайн-теста недопустим: без VPN это задержка, с VPN — запрос к стенду.
    Тесты самого Goods помечены @pytest.mark.goods и подменяют сеть сами.
    """
    if request.node.get_closest_marker("goods"):
        return
    monkeypatch.setattr("lo_api.picking.PickingClient.goods_card", lambda self, task: None)


@pytest.fixture
def cfg() -> Settings:
    # Паузы обнулены: оффлайн-тесты не должны ждать реальные секунды
    return Settings(_env_file=None, accounts=[], skus=[], retry_attempts=1,
                    sap_validate_delay=0, order_poll_interval=0)


# --- конфиг ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9990001122", "79990001122"),
        ("79990001122", "79990001122"),
        ("+7 999 000-11-22", "79990001122"),
        ("89990001122", "79990001122"),
        ("8 (999) 000 11 22", "79990001122"),
    ],
)
def test_phone_normalization(raw: str, expected: str):
    assert Account(phone=raw).phone == expected


@pytest.mark.parametrize("raw", ["123", "abcdefghij", "", "94300000141234"])
def test_invalid_phone_rejected(raw: str):
    with pytest.raises(ValueError):
        Account(phone=raw)


def test_mpk_follows_mission(cfg: Settings):
    ctx = Context(cfg=cfg, mission=Mission.PICKUP)
    assert ctx.mpk == cfg.mpk_pickup
    ctx.mission = Mission.DELIVERY
    assert ctx.mpk == cfg.mpk_delivery, (
        "MPK должен следовать за миссией — в старом коде он рассинхронизировался"
    )


def test_sku_lookup_by_kind(cfg: Settings):
    cfg.skus = [Sku(id="1", kind=SkuKind.REGULAR), Sku(id="2", kind=SkuKind.ALCOHOL)]
    assert cfg.sku_of(SkuKind.ALCOHOL).id == "2"
    assert cfg.sku_of(SkuKind.WEIGHT) is None, "ненастроенная категория → None, а не ошибка"


# --- заголовки -------------------------------------------------------------
def test_no_cookie_headers_anywhere(cfg: Settings):
    """Подтверждено: Head.SessionToken достаточно. Cookie быть не должно."""
    ctx = Context(cfg=cfg, session_token="TOKEN", passport_id="PID", legal_entity_id="ORG")
    t = Transport(ctx)
    all_headers = {**t._session.headers, **t._dynamic_headers()}
    assert not any(k.lower() == "cookie" for k in all_headers)


def test_identity_headers_come_from_context(cfg: Settings):
    """Регресс: раньше passport-id и organization-id были литералами."""
    ctx = Context(cfg=cfg, session_token="TOKEN", passport_id="PID", legal_entity_id="ORG")
    h = Transport(ctx)._dynamic_headers()
    assert h["x-passport-id"] == "PID"
    assert h["x-organization-id"] == "ORG"
    assert h["sessiontoken"] == "TOKEN"


def test_unauthorized_context_sends_no_identity(cfg: Settings):
    h = Transport(Context(cfg=cfg))._dynamic_headers()
    assert "sessiontoken" not in h
    assert "x-passport-id" not in h


def test_trace_headers_are_unique_per_request(cfg: Settings, monkeypatch):
    seen: list[str] = []

    def fake_request(self, method, url, **kw):
        seen.append(kw["headers"]["x-trace-id"])
        return _resp(200, {"Body": {}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    t = Transport(Context(cfg=cfg))
    t.rest("cartLookup")
    t.rest("cartLookup")

    assert len(set(seen)) == 2, "trace_id должен быть свой у каждого запроса"


# --- конверт ---------------------------------------------------------------
def _resp(status: int, payload=None, *, text: str | None = None,
          ensure_ascii: bool = False) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    body = text if text is not None else json.dumps(payload, ensure_ascii=ensure_ascii)
    r._content = body.encode()
    r.headers["Content-Type"] = "application/json"
    return r


def test_rest_envelope_shape(cfg: Settings, monkeypatch):
    captured: dict = {}

    def fake_request(self, method, url, **kw):
        captured["url"] = url
        captured["body"] = json.loads(kw["data"]["request"])
        return _resp(200, {"Body": {"Goods": []}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    ctx = Context(cfg=cfg, session_token="TOKEN", mission=Mission.DELIVERY)
    Transport(ctx).rest("cartLookup", {"Return": {"Goods": 1}})

    head = captured["body"]["Head"]
    assert captured["url"].endswith("/api/rest/cartLookup")
    assert head["Method"] == "cartLookup"
    assert head["SessionToken"] == "TOKEN"
    assert head["MarketingPartnerKey"] == cfg.mpk_delivery
    assert head["Version"] == CLIENT_VERSION
    assert head["RequestId"].startswith("cartLookup_")
    assert captured["body"]["Body"] == {"Return": {"Goods": 1}}


def test_request_id_is_unique(cfg: Settings, monkeypatch):
    ids: list[str] = []

    def fake_request(self, method, url, **kw):
        ids.append(json.loads(kw["data"]["request"])["Head"]["RequestId"])
        return _resp(200, {"Body": {}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    t = Transport(Context(cfg=cfg))
    t.rest("cartLookup")
    t.rest("cartLookup")
    assert ids[0] != ids[1]


# --- разбор ошибок ---------------------------------------------------------
def test_business_error_on_http_200(cfg: Settings, monkeypatch):
    """Ключевая дыра старого скрипта: 200 с ошибкой в теле проезжал молча."""
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Head": {"Error": "GOODS_NOT_AVAILABLE"}}),
    )
    with pytest.raises(BusinessError, match="GOODS_NOT_AVAILABLE"):
        Transport(Context(cfg=cfg)).rest("cartItemModify")


def test_error_list_becomes_business_error(cfg: Settings, monkeypatch):
    """Стенд объясняет отказ через Body.ErrorList, оставаясь на 200.

    Так приходит «Все курьеры пока заняты» вместо списка интервалов. Без этой
    проверки причина терялась и вызывающий код сообщал лишь «список пуст»,
    отправляя разбираться не туда.
    """
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Body": {
            "DeliveryPlannedProIntervalList": [],
            "ErrorList": [{"Code": 3, "Message": "Все курьеры пока заняты"}],
        }}),
    )
    with pytest.raises(BusinessError, match="Все курьеры пока заняты"):
        Transport(Context(cfg=cfg)).rest("userAddressDeliveryIntervalSearchsynced")


def test_mission_is_chosen_interactively(cfg: Settings, monkeypatch):
    """Миссия спрашивается, когда --mission не передан.

    С -y и в неинтерактивном режиме вопрос не задаётся — берётся доставка,
    иначе прогон в CI завис бы на вводе.
    """
    from unittest.mock import MagicMock

    from lo_api.cli.order import build_params, build_parser

    api = MagicMock()
    api.address.of_kind.return_value = [Address(Id=1, StreetTitle="ул А", HouseNumber="1")]
    # Магазин у самовывоза спрашивается так же, как адрес у доставки;
    # когда он один, вопроса нет
    api.store.pickup.return_value = [Store(id=3429, title="ТК12")]

    def build(argv: list[str], answers: list[str]) -> Mission:
        monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))
        return build_params(api, cfg, build_parser().parse_args(argv)).mission

    assert build(["--sku", "1"], ["2"]) is Mission.PICKUP
    assert build(["--sku", "1"], ["1", ""]) is Mission.DELIVERY
    assert build(["--sku", "1"], ["", ""]) is Mission.DELIVERY  # Enter — доставка
    assert build(["--sku", "1", "--mission", "pickup"], []) is Mission.PICKUP
    assert build(["--sku", "1", "-y"], []) is Mission.DELIVERY


def test_broken_json_in_env_points_to_the_place(tmp_path, monkeypatch):
    """Сломанный JSON в .env называет файл, место и скелет строки.

    pydantic сообщает только «error parsing value for field accounts». В
    LO_ACCOUNTS у второго аккаунта потерялись обе фигурные скобки, а метки
    были с пробелами — и поломка выглядела как «label не принимает пробелы».
    """
    from lo_api.config import load_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LO_STAND", raising=False)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "test12.env").write_text("LO_WORKSHIFT_TK=43\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        'LO_STAND=test12\nLO_ACCOUNTS=[{"phone":"79990001122","label":"ООО ОДИН"},'
        '"phone":"79990003344","label":"ООО ДВА"]\n', encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        load_settings()
    text = str(caught.value)
    assert "LO_ACCOUNTS в .env" in text
    assert '[{"…":"…","…":"…"},"…":"…","…":"…"]' in text, "скелет без содержимого строк"
    assert "фигурная скобка" in text

    # Пробелы в метках сами по себе ничего не ломают
    (tmp_path / ".env").write_text(
        'LO_STAND=test12\nLO_ACCOUNTS=[{"phone":"79990001122","label":"ООО ОДИН"},'
        '{"phone":"79990003344","label":"ООО ДВА"}]\n', encoding="utf-8")
    assert [a.label for a in load_settings().accounts] == ["ООО ОДИН", "ООО ДВА"]


def test_phone_is_chosen_from_accounts_or_typed(cfg: Settings, monkeypatch):
    """Номер не зашит умолчанием в вопрос: список из LO_ACCOUNTS плюс ручной ввод.

    Последний пункт меню — свой номер, он проверяется тем же нормализатором,
    что и конфиг. Без аккаунтов в конфиге меню не строится, номер спрашивается
    сразу. С -y берётся первый из списка — прогон в CI не должен ждать ввода.
    """
    from lo_api.cli.order import build_parser, choose_phone

    cfg.accounts = [Account(phone="79990001122", label="основной"),
                    Account(phone="79990003344")]

    def pick(argv: list[str], answers: list[str]) -> str:
        monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))
        return choose_phone(cfg, build_parser().parse_args(argv))

    assert pick([], [""]) == "79990001122", "Enter — первый в списке"
    assert pick([], ["2"]) == "79990003344"
    # «Ввести вручную»: пустой и кривой ответ не принимаются, номер нормализуется
    assert pick([], ["3", "", "123", "8 (999) 000 55 66"]) == "79990005566"
    assert pick(["--phone", "89990007788"], []) == "79990007788"
    assert pick(["-y"], []) == "79990001122"

    cfg.accounts = []
    assert pick([], ["9990009900"]) == "79990009900", "без списка — сразу ввод"


def test_marking_barcode_normalization():
    """Штрихкод приводится к 13 цифрам, SKU от штрихкода отличается.

    Генераторы подставляют `010` + 13 цифр, то есть GTIN-13 с ведущим нулём.
    Опечатка в длинном коде не должна превращать его в SKU: иначе вместо
    «проверьте цифры» человек получит «товар не найден».
    """
    from lo_api.marking import looks_like_barcode, to_marking_barcode

    assert to_marking_barcode("4600680001975") == "4600680001975"
    assert to_marking_barcode("04600680001975") == "4600680001975", "GTIN-14 с нулём"
    assert to_marking_barcode("96385074") == "0000096385074", "EAN-8"

    assert not looks_like_barcode("520787") and not looks_like_barcode("0520787")
    assert looks_like_barcode("4600680001976"), "опечатка — всё равно штрихкод"
    with pytest.raises(ValueError, match="контрольная цифра"):
        to_marking_barcode("4600680001976")
    with pytest.raises(ValueError, match="не с нуля"):
        to_marking_barcode("14600680001972")


def test_mark_mode_finds_barcode_by_sku(cfg: Settings, monkeypatch, capsys):
    """По SKU штрихкод берётся из Goods, весовые коды отбрасываются.

    Группа — из LO_MARKING_TYPES[sku], если задана. В выводе и видимая форма
    с <GS>, и JSON с настоящим escape — его вставляют в тела Postman/Bruno.
    """
    from lo_api.cli.order import build_parser, run_mark_only
    from lo_api.goods import piece_barcodes

    assert piece_barcodes([{"code": "2400138000000", "type": "Z2"},
                           {"code": "4600680001975", "type": "EAN13"}]) == ["4600680001975"]

    asked = {}

    def fake_get(self, url, **kw):
        asked["url"] = url
        return _resp(200, {"item": {"barcodes": [
            {"code": "2400138000000", "type": "Z2"},
            {"code": "4600680001975", "type": "EAN13"}]}})

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: False)
    cfg.goods_store_id = 12
    cfg.marking_types = {"520787": "chemistry"}

    args = build_parser().parse_args(["--mode", "mark", "--item", "0520787", "--count", "2"])
    assert run_mark_only(cfg, args) == 0

    assert asked["url"].endswith("/v2/gate/item/CHANNEL_B2B/12/520787"), "sku без нулей"
    out = capsys.readouterr().out
    assert "chemistry_short, штрихкод 4600680001975, sku 520787" in out
    assert out.count("0104600680001975215") == 4, "две марки, каждая в двух видах"
    assert "<GS>" in out and "\\u001d" in out
    assert "\x1d" not in out, "невидимый разделитель в терминал не печатается"


def test_mark_copy_puts_real_gs_to_clipboard(monkeypatch):
    """В буфер уходит марка с настоящим U+001D — из терминала его не скопировать."""
    from lo_api.cli import order

    got = {}
    monkeypatch.setattr(order.shutil, "which",
                        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    monkeypatch.setattr(order.subprocess, "run",
                        lambda cmd, **kw: got.update(cmd=cmd, data=kw["input"]))
    order._copy_to_clipboard("010460068000197521abc\x1d93xyz")
    assert got["cmd"] == ["/usr/bin/pbcopy"]
    assert b"\x1d" in got["data"]


def test_own_needs_respects_what_the_stand_allows(cfg: Settings, monkeypatch):
    """Менять можно только разрешённое, и лишний Modify не отправляется.

    Тела запросов и ответов — из HAR «СН для заказов». Признак, закрытый
    `abilityMarkOwnNeeds=false` или `*ReadOnly=true`, не уходит на стенд:
    в веб-морде такая галочка просто неактивна.
    """
    from lo_api.flows import CheckoutFlow, CheckoutParams

    lookup = {"abilityVetOwnNeeds": True, "abilityMarkOwnNeeds": True,
              "abilityVetOwnNeedsReadOnly": False, "abilityMarkOwnNeedsReadOnly": False,
              "isVetOwnNeeds": True, "isMarkOwnNeeds": False,
              "hasVetGoods": True, "hasMarkGoods": True,
              "messageOwnNeeds": "Заказ ветеринарной продукции будет в собственные нужды"}

    def run(state: dict, **wanted) -> list[dict]:
        sent: list[dict] = []

        def fake(self, method, url, **kw):
            body = json.loads(kw["data"])
            sent.append(body)
            result = dict(state)
            if body["method"] == "proStateRegulationModify":
                result["isVetOwnNeeds"] = body["params"]["isVetOwnNeeds"]
                result["isMarkOwnNeeds"] = body["params"]["isMarkOwnNeeds"]
            return _resp(200, {"jsonrpc": "2.0", "result": result})

        monkeypatch.setattr(requests.Session, "request", fake)
        flow = CheckoutFlow(LentaApi(Context(cfg=cfg, session_token="T")))
        flow.set_own_needs(CheckoutParams(mission=Mission.DELIVERY, items=[], **wanted))
        return sent

    # Ничего не просили — один lookup, Modify нет
    assert [b["method"] for b in run(lookup)] == ["proStateRegulationLookup"]

    # Просимое совпадает с текущим — тоже незачем
    assert [b["method"] for b in run(lookup, vet_own_needs=True)] == \
        ["proStateRegulationLookup"]

    # Настоящее изменение: оба признака едут вместе, как шлёт браузер
    sent = run(lookup, mark_own_needs=True)
    assert [b["method"] for b in sent][-1] == "proStateRegulationModify"
    assert sent[-1]["params"] == {"OrganizationId": 1, "Count": 1,
                                  "isVetOwnNeeds": True, "isMarkOwnNeeds": True}

    # Признак закрыт — на стенд не уходит, даже если попросили
    closed = {**lookup, "abilityMarkOwnNeeds": False}
    assert [b["method"] for b in run(closed, mark_own_needs=True)] == \
        ["proStateRegulationLookup"]
    read_only = {**lookup, "abilityMarkOwnNeedsReadOnly": True}
    assert [b["method"] for b in run(read_only, mark_own_needs=True)] == \
        ["proStateRegulationLookup"]


def test_store_client_asks_the_stand(cfg: Settings, monkeypatch):
    """Клиент магазинов и правда ходит на стенд — с пустым телом и по POST.

    Тест выбора магазина мокает api.store.pickup целиком, поэтому сам вызов
    там не проверяется: из-за этого опечатка в имени параметра транспорта
    (`json` вместо `json_body`) всплыла только на живом прогоне.
    """
    sent = {}

    def fake(self, method, url, **kw):
        sent.update(method=method, url=url, data=kw.get("data"))
        # Форма ответа — из HAR стенда
        return _resp(200, {"items": [
            {"id": 3429, "title": "ТК12", "alias": "0012", "marketType": "SM",
             "addressShort": "Санкт-Петербург, Хасанская ул., 17/1",
             "addressFull": "Санкт-Петербург, Хасанская улица, 17/1", "selected": True},
        ]})

    monkeypatch.setattr(requests.Session, "request", fake)
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    stores = api.store.pickup()

    assert sent["method"] == "POST"
    assert sent["url"].endswith("/api-gateway/v1/stores/pickup/search")
    assert sent["data"] == "{}", "тело пустое, список зависит только от аккаунта"
    assert [s.id for s in stores] == [3429]
    assert str(stores[0]).startswith("ТК12 — Санкт-Петербург")


def test_pickup_store_is_chosen_from_available(cfg: Settings, monkeypatch):
    """Магазин самовывоза берётся из списка доступных, а не вслепую из конфига.

    id магазина в stores/pickup/search — это и есть storeId заказа, поэтому
    сопоставлять его с alias («0012») или названием («ТК12») не нужно.
    """
    from unittest.mock import MagicMock

    from lo_api.cli.order import build_params, build_parser

    stores = [Store(id=108, title="ТК21", addressShort="Череповец"),
              Store(id=3429, title="ТК12", addressShort="Санкт-Петербург"),
              Store(id=3149, title="ТК124", addressShort="Москва", selected=True)]
    api = MagicMock()
    api.store.pickup.return_value = stores

    def build(argv: list[str], answers: list[str]) -> int | None:
        monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))
        return build_params(api, cfg, build_parser().parse_args(argv)).store_id

    base = ["--sku", "1", "--mission", "pickup"]
    assert build(base, ["1"]) == 108, "выбран первый пункт списка"
    # Enter — тот магазин, который стенд пометил выбранным
    assert build(base, [""]) == 3149
    # --store-id не спрашивает вовсе: список для этого даже не читается
    assert build([*base, "--store-id", "3491"], []) == 3491

    # Без диалога умолчание из конфига, если стенд никого не выбрал
    for s in stores:
        s.selected = False
    cfg.pickup_store_id = 3429
    assert build([*base, "-y"], []) == 3429


def test_enter_picks_first_option(monkeypatch):
    """Enter выбирает первый пункт меню.

    В подтверждении заказа первым пунктом стоит создание, поэтому Enter
    оформляет заказ, а отмена требует явной «2». Раньше вопрос был свободным
    вводом y/N, и ответ в кириллице молча читался как отказ — заказ не
    создавался при явном согласии.
    """
    from lo_api.cli.order import _choose

    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert _choose("Создать заказ?", ["Да, создать", "Нет, отменить"]) == 0

    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert _choose("Создать заказ?", ["Да, создать", "Нет, отменить"]) == 1


def test_interval_chosen_by_id_and_by_index():
    """Интервал выбирается точечно по Id, недоступные не предлагаются."""
    from lo_api.cli.order import build_parser, choose_interval

    intervals = [
        Interval(Id="A1", Date="2026-09-07", TimeFrom="18:00:00", TimeTo="18:30:00",
                 Disabled=False),
        Interval(Id="A2", Date="2026-09-07", TimeFrom="18:30:00", TimeTo="19:00:00",
                 Disabled=False),
        Interval(Id="X", Date="2026-09-07", TimeFrom="19:00:00", TimeTo="19:30:00",
                 Disabled=True),
    ]
    by_id = choose_interval(intervals, build_parser().parse_args(["--interval-id", "A2"]))
    assert str(by_id.id) == "A2"

    by_index = choose_interval(intervals, build_parser().parse_args(["-y", "--interval", "1"]))
    assert str(by_index.id) == "A2"

    with pytest.raises(SystemExit, match="недоступен"):
        choose_interval(intervals, build_parser().parse_args(["--interval-id", "X"]))


TMS_FORM = """
<form method="post">
  <input type="hidden" name="csrfmiddlewaretoken" value="TOKEN123">
  <select name="new_picker">
    <option value="">---</option>
    <option value="111">Dummy (111)</option>
    <option value="95371423">Пикерок БиТуБи (95371423)</option>
    <option value="222">Dummy (222)</option>
  </select>
  <select name="new_couriers">
    <option value="333">Dummy (333)</option>
    <option value="95371418">Курьерыч БиТуБи (95371418)</option>
  </select>
  <input type="submit" name="action" value="Сохранить изменения">
</form>
"""


def test_tms_form_parsing_puts_real_performers_first():
    """Заглушки не должны попадать в назначение.

    В форме TMS три десятка вариантов, из них настоящие — по одному на роль,
    остальные Dummy. Выбор первого попавшегося назначил бы заглушку.
    """
    from lo_api.tms import OrderPage, _csrf, _options, _submit_action

    assert _csrf(TMS_FORM) == "TOKEN123"
    assert _submit_action(TMS_FORM) == "Сохранить изменения"

    page = OrderPage(_csrf(TMS_FORM), _submit_action(TMS_FORM),
                     _options(TMS_FORM, "new_picker"), _options(TMS_FORM, "new_couriers"))
    # Пустой option отбрасывается, иначе назначением стало бы «не переносить»
    assert [p.id for p in page.pickers] == ["111", "95371423", "222"]
    assert page.picker_choices[0].id == "95371423"
    assert page.courier_choices[0].id == "95371418"
    assert page.picker_choices[0].is_dummy is False


def test_all_dummy_means_nobody_on_shift():
    """Сплошные Dummy = смену никто не открыл, назначать некого.

    Сотрудник появляется в списке TMS, только когда вывел себя на смену через
    picker-service. Без этой проверки заказ назначался бы на заглушку.
    """
    from lo_api.tms import OrderPage, Performer

    real = Performer("95371423", "Пикерок БиТуБи (95371423)")
    dummy = Performer("111", "Dummy (111)")

    assert OrderPage("t", "a", [real], [real]).has_real_performers
    assert not OrderPage("t", "a", [dummy], [real]).has_real_performers
    assert not OrderPage("t", "a", [real], [dummy]).has_real_performers
    assert not OrderPage("t", "a", [dummy], [dummy]).has_real_performers


def test_workshift_sequences_match_collection(cfg: Settings, monkeypatch):
    """Порядок шагов совпадает с коллекцией B2B-Pick.

    У курьера первым идёт вызов с пустыми params — он открывает смену, и без
    него последующие key/value отвергаются. У сборщика вместо него обед.
    """
    from lo_api.workshift import WorkshiftClient

    calls: list[tuple[str, dict]] = []

    def fake_post(self, url, json=None, headers=None, **kw):
        calls.append((json["method"], json["params"]))
        return _resp(200, {"result": {"token": "T" * 32}})

    monkeypatch.setattr(requests.Session, "post", fake_post)
    cfg.picker_login, cfg.picker_password = "95371423", "x"
    cfg.courier_login, cfg.courier_password = "95371418", "x"

    WorkshiftClient(cfg).start_picker()
    assert [p.get("key") for _, p in calls[1:]] == ["tk", "workshift_end", "lunch", "enable"]

    calls.clear()
    WorkshiftClient(cfg).start_courier()
    assert calls[1] == ("staffWorkshiftStart_v2", {}), "первым идёт пустой вызов"
    assert [p.get("key") for _, p in calls[2:]] == ["tk", "workshift_end", "enable"]


def test_log_file_is_renamed_after_order(tmp_path):
    """Лог прогона переименовывается в «<номер заказа>.log».

    Номер известен только после orderAdd, поэтому файл заводится под временным
    именем — записанное до переименования обязано сохраниться.
    """
    import logging

    from lo_api.cli.order import name_log_after_order, setup_logging

    log_dir = tmp_path / "logs"
    try:
        setup_logging(verbose=False, log_file="", log_dir=str(log_dir))
        logging.getLogger("lo_api.http").info("до появления номера")
        name_log_after_order("900001126135")
        logging.getLogger("lo_api.http").info("после переименования")

        written = list(log_dir.iterdir())
        assert [p.name for p in written] == ["900001126135.log"], "временный файл не убран"
        text = written[0].read_text(encoding="utf-8")
        assert "до появления номера" in text
        assert "после переименования" in text
    finally:
        logging.getLogger().handlers.clear()


def test_explicit_log_file_is_not_renamed(tmp_path):
    """Явно заданный --log-file остаётся с выбранным именем."""
    import logging

    from lo_api.cli.order import name_log_after_order, setup_logging

    chosen = tmp_path / "my.log"
    try:
        setup_logging(verbose=False, log_file=str(chosen), log_dir="")
        logging.getLogger("lo_api.http").info("строка")
        name_log_after_order("900001126135")
        assert chosen.exists()
        assert not (tmp_path / "900001126135.log").exists()
    finally:
        logging.getLogger().handlers.clear()


def test_marking_formats_match_collection():
    """Форматы марок перенесены дословно: сервер разбирает их по длинам полей."""
    import re

    from lo_api.marking import GS, generate_marking

    bc = "4600000000017"
    gs = re.escape(GS)
    cases = {
        "milk": rf"^010{bc}21\w{{6}}{gs}93\w{{4}}$",
        "shoes": rf"^010{bc}21\w{{13}}{gs}91\w{{4}}{gs}92\w{{88}}$",
        "beer": rf"^010{bc}215\w{{6}}{gs}91\w{{4}}$",
        "bio_long": rf"^010{bc}215\w{{12}}{gs}91\w{{4}}{gs}92\w{{44}}$",
        "electronics": rf"^010{bc}21\w{{20}}{gs}91\w{{4}}{gs}92\w{{44}}$",
        # табачная пачка идёт без префикса 010 и без разделителей
        "tobacco_consumer_package": rf"^{bc}\w{{15}}$",
    }
    for kind, pattern in cases.items():
        assert re.match(pattern, generate_marking(bc, kind)), kind

    # Имя функции из коллекции принимается как есть
    assert generate_marking(bc, "markingBio_short").startswith(f"010{bc}215")
    with pytest.raises(ValueError, match="нет генератора марки"):
        generate_marking(bc, "нетакой")


def test_marking_kind_accepts_group_without_suffix():
    """Группу можно задать как «SEAFOOD» — без суффикса длины и в любом регистре.

    У части групп есть два варианта марки, _short и _long; когда суффикс не
    указан, берётся короткая — она встречается чаще.
    """
    from lo_api.marking import resolve_marking_kind

    assert resolve_marking_kind("SEAFOOD") == "seafood_short"
    assert resolve_marking_kind("markingSeafood_long") == "seafood_long"
    assert resolve_marking_kind("MILK") == "milk"
    assert resolve_marking_kind("Bio_short") == "bio_short"

    # Незнакомая группа: сообщение не вываливает все 47 имён подряд
    with pytest.raises(ValueError, match="нет генератора марки"):
        resolve_marking_kind("сыр")


def test_gs_separator_is_control_character():
    """Разделитель в марке — настоящий U+001D, а не шесть печатных символов.

    В коллекции gs = '\\\\u001D', но Bruno подставляет марку текстом в шаблон
    JSON-тела, где это обычный JSON-escape: до сервера доезжает управляющий
    символ. На шесть символов буквально стенд отвечает «Не удалось получить
    короткую марку для марки …».
    """
    import json

    from lo_api.marking import GS, generate_marking

    assert GS == "\x1d"
    assert len(GS) == 1

    # В теле запроса разделитель обязан стать JSON-escape \\u001d, а не
    # экранированным обратным слешем
    body = json.dumps({"marking": generate_marking("4600000000017", "milk")})
    assert "\\u001d" in body
    assert "\\\\u001d" not in body


def test_weight_barcode_carries_grams():
    """Вес зашит в ШК; контрольная цифра пересчитана."""
    from lo_api.marking import weight_barcode, weight_barcode_issues

    code = weight_barcode("2400138000000", 1234)
    assert len(code) == 13
    assert code[7:12] == "01234"
    assert weight_barcode_issues(code, 1234) == []
    # Расхождение веса и битая контрольная цифра называются прямо
    assert "вес в ШК" in "; ".join(weight_barcode_issues(code, 999))
    assert "не 13 цифр" in "; ".join(weight_barcode_issues("123", 1))


def test_ean13_generation():
    from lo_api.marking import generate_ean13, is_valid_ean13

    assert is_valid_ean13(generate_ean13("2400138000000"))
    # Уже корректный код не трогается
    valid = generate_ean13("2400138000000")
    assert generate_ean13(valid) == valid


def test_picking_classifies_items():
    """Типы позиций — по приоритету признаков эталонного документа (раздел 3.4).

    isMarking → isBatchMarking → controlType A/V → isOsuMarkRequired. Партионный
    учёт — это A (алкоголь) ИЛИ V (вет): раньше проверялся только V, и пиво
    собиралось как ПЭУ без партий.
    """
    from lo_api.picking import classify

    assert classify({"sku": "520787"}) == "SIMPLE"
    assert classify({"controlType": "V"}) == "BATCH"
    assert classify({"controlType": "A"}) == "BATCH", "алкоголь — тоже партионный"
    assert classify({"isMarking": True}) == "MARK_PEU"
    # ПЭУ важнее ОСУ: isMarking стоит выше isOsuMarkRequired
    assert classify({"isMarking": True, "isOsuMarkRequired": True}) == "MARK_PEU"
    assert classify({"isMarkingInGoods": True, "controlType": "V"}) == "MARK_PEU_BATCH"
    # Пиво ПЭУ: controlType A
    assert classify({"isMarkingInGoods": True, "controlType": "A"}) == "MARK_PEU_BATCH"
    assert classify({"isMarking": True, "controlType": "A"}) == "MARK_PEU_BATCH"
    # isBatchMarking — ветка «ПЭУ с партиями», даже если controlType не пришёл
    assert classify({"isMarkingInGoods": True, "isBatchMarking": True}) == "MARK_PEU_BATCH"
    # До сборки isMarking = null: ОСУ определяется по своему признаку, как раньше
    assert classify({"isMarking": None, "isMarkingInGoods": True,
                     "isOsuMarkRequired": True, "controlType": "V"}) == "MARK_OSU_BATCH"
    assert classify({"isMarkingInGoods": True, "isOsuMarkRequired": True}) == "MARK_OSU"
    # Весовой — не тип, а модификатор: тип у него определяется как обычно
    assert classify({"unit": "GW"}) == "SIMPLE"
    assert classify({"unit": "GW", "isMarking": True}) == "MARK_PEU"
    assert classify({"sampleId": 7}) == "SAMPLE"
    assert classify({"sampleId": 7, "sku": "1"}) == "SAMPLE_SKU"


def test_picking_plan_splits_by_warehouse():
    """Позиция, разложенная по складам, собирается по шагу на склад.

    stockId и storeId у шагов разные — иначе скан уйдёт не на тот склад.
    Ведущие нули в sku срезаются: методы сборки ждут его без них.
    """
    from lo_api.picking import build_plan

    plan = build_plan({
        "warehouses": [{"id": 1, "storeId": 3429}],
        "itemsList": [{
            "sku": "0520787", "barcode": "4600000000017", "name": "Кофе",
            "stockData": {"records": [
                {"count": 2, "id": 7777, "storeId": 3429},
                {"count": 1, "id": 1, "storeId": 3429},
            ]},
        }],
    })
    assert not plan.unsupported
    assert [(t.sku, t.count, t.stock_id) for t in plan.tasks] == [
        ("520787", 2, 7777), ("520787", 1, 1),
    ]


def test_order_get_unwraps_result_order(cfg: Settings, monkeypatch):
    """Заказ лежит в result.order, а не в result напрямую.

    Возврат result целиком давал пустой itemsList и «нечего собирать» при
    непустом заказе.
    """
    from lo_api.picking import PickingClient

    monkeypatch.setattr(
        requests.Session, "post",
        lambda self, url, json=None, headers=None, **kw: _resp(200, {
            "result": {"order": {"id": "900001", "itemsList": [{"sku": "1"}]}},
        }),
    )
    order = PickingClient(cfg).order_get("900001")
    assert order["itemsList"] == [{"sku": "1"}]


def test_order_get_reports_contract_change(cfg: Settings, monkeypatch):
    """Пропавший result.order называется прямо, а не превращается в пустой заказ."""
    from lo_api.picking import PickingClient

    monkeypatch.setattr(
        requests.Session, "post",
        lambda self, url, json=None, headers=None, **kw: _resp(200, {"result": {"data": {}}}),
    )
    with pytest.raises(LoApiError, match="контракт метода изменился"):
        PickingClient(cfg).order_get("900001")


def test_picking_branches_per_item_kind(cfg: Settings, monkeypatch):
    """Каждый тип позиции идёт своей веткой — как в ROUTE из flow.js.

    Ключевое: MARK_PEU не сканируется вовсе (марки снимаются сразу), а
    MARK_PEU_BATCH заводит партии через sapBatchAdd и снимает марки батчевым
    методом. Перепутать ветки — значит оставить заказ недособранным.
    """
    from lo_api.picking import PickingClient, pick_order

    cfg.batch_date = "2026-02-26"
    order = {
        "warehouses": [{"id": 1, "storeId": 3429}],
        "statusInfo": {"alias": "ACCEPTED"},
        "itemsList": [
            {"sku": "1", "barcode": "4600000000017", "count": 2, "isMarking": True},
            {"sku": "2", "barcode": "4600000000024", "count": 1, "controlType": "V"},
            {"sku": "3", "barcode": "4600000000031", "count": 1,
             "isMarking": True, "controlType": "V"},
            {"sku": "4", "barcode": "4600000000048", "count": 2},
        ],
    }
    calls: list[str] = []
    seen: list[int] = []

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        calls.append(method)
        if method == "orderGet":
            seen.append(1)
            alias = "ACCEPTED" if len(seen) == 1 else "SAP_VALIDATING_SUCCESS"
            # После сборки позиции засчитаны — иначе сработает проверка полноты
            items = [{**i, "countPick": i["count"]} for i in order["itemsList"]]
            return _resp(200, {"result": {"order": {
                **order, "itemsList": items, "statusInfo": {"alias": alias}}}})
        if method == "sapManufacturer":
            return _resp(200, {"result": {"manufacturers": [
                {"code": "X1", "name": "Другой", "inn": "1"},
                {"code": "PP", "name": "ПП для Ленты", "inn": "2"},
            ]}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    pick_order(client, "900001")

    # MARK_PEU: две единицы — две марки и ни одного скана по нему
    assert calls.count("itemIncreaseByMarking") == 2
    assert calls.count("itemUpdateByBarcode") == 3, "MARK_PEU не сканируется"
    assert calls.count("sapBatch") == 1        # BATCH
    assert calls.count("sapBatchAdd") == 1     # MARK_PEU_BATCH
    assert calls.count("itemBatchIncreaseByMarking") == 1
    assert calls[-2:] == ["getPackages", "orderUpdate"]


def test_network_error_does_not_kill_session_cache(cfg: Settings, monkeypatch):
    """Нет связи со стендом — это не протухший токен.

    Раньше пропавший VPN трактовался как недействительная сессия: кэш стирался,
    и следующий прогон зря тратил попытку входа по коду.
    """
    api = LentaApi(Context(cfg=cfg, session_token="TOKEN"))

    def no_network(self, method, url, **kw):
        raise requests.ConnectionError("Failed to resolve host")

    monkeypatch.setattr(requests.Session, "request", no_network)
    with pytest.raises(HttpStatusError, match="сетевая ошибка"):
        api.auth.is_session_alive()

    # А вот 401 — настоящий признак протухшего токена
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _resp(401, {"error": "unauthorized"}))
    assert api.auth.is_session_alive() is False


def test_pickup_order_is_recognized():
    """Самовывоз узнаётся по любому из трёх признаков коллекции.

    Номер заказа на 462, «Самовывоз» в названии интервала или pickup в его
    типе. Доставка ни по одному не подходит.
    """
    from lo_api.picking import is_pickup_order

    assert is_pickup_order({"id": "4621169458"})
    assert is_pickup_order({"hubOrderId": "4621169458"})
    assert is_pickup_order({"printableInfo": {"orderNumber": "462116-9458"}})
    assert is_pickup_order({"deliveryInfo": {"deliveryIntervalName": "Самовывоз ТК12"}})
    assert is_pickup_order({"deliveryInfo": {"deliveryIntervalType": "pickup"}})

    # Реальный заказ доставки из лога
    assert not is_pickup_order({
        "id": "900001126144", "hubOrderId": "900001126144",
        "deliveryInfo": {"deliveryIntervalName": "Плановая b2b",
                         "deliveryIntervalType": "planned_pro"},
    })


def test_pickup_skips_courier_steps(cfg: Settings, monkeypatch):
    """Самовывоз не идёт по флоу доставки: его путь кончается на сборке."""
    from lo_api.picking import PickingClient, deliver_order, pick_order

    order = {"id": "4621169458", "hubOrderId": "4621169458",
             "statusInfo": {"alias": "ASSEMBLE_COMPLETE"},
             "deliveryInfo": {"deliveryIntervalName": "Самовывоз"}}
    calls: list[str] = []

    def fake(self, url, json=None, headers=None, **kw):
        calls.append(json["method"])
        if json["method"] == "orderGet":
            return _resp(200, {"result": {"order": order}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"

    # Скомплектованный самовывоз — это «готово», а не «дальше курьер»
    assert pick_order(client, "4621169458") == "done"

    calls.clear()
    deliver_order(client, "4621169458")
    assert "orderUpdate" not in calls, "статусы курьера самовывозу не ставятся"


def test_picked_issues_finds_undercollected():
    """Главная проверка прогона: товар реально в заказе, а не «методы не ругнулись».

    У весового countPick приходит фактическим весом в тех же единицах, что
    count, с допуском 10% на довесок. Пробы не проверяются.
    """
    from lo_api.picking import picked_issues

    assert picked_issues({"itemsList": [{"sku": "1", "count": 2, "countPick": 2}]}) == []
    assert picked_issues({"itemsList": [{"sku": "1", "count": 2, "countPick": 1}]}) == [
        "1: 1/2 шт"
    ]
    # Весовой: 950 г вместо 1000 — в пределах допуска
    assert picked_issues({"itemsList": [
        {"sku": "2", "unit": "GW", "count": 1000, "countPick": 950}]}) == []
    assert picked_issues({"itemsList": [
        {"sku": "2", "unit": "GW", "count": 1000, "countPick": 500}]}) != []
    # Заказ 900001126147: 2200 г из 4 порций, собрано 4 г — ровно по числу
    # порций. Послабление «countPick может прийти порциями» такое считало
    # нормой, и недосбор в 550 раз проходил проверку молча
    assert picked_issues({"itemsList": [
        {"sku": "181253", "unit": "GW", "count": 2200, "portion": 4,
         "countPick": 4}]}) == ["181253: 4/2200 г"]
    # Проба не проверяется
    assert picked_issues({"itemsList": [
        {"sku": "4", "sampleId": 7, "count": 1, "countPick": 0}]}) == []


def test_validation_fails_when_order_undercollected(cfg: Settings, monkeypatch):
    """Валидация прошла, но товар не засчитан — прогон не должен быть зелёным."""
    from lo_api.picking import PickingClient, _validate_sap

    monkeypatch.setattr(
        requests.Session, "post",
        lambda self, url, json=None, headers=None, **kw: _resp(200, {"result": {"order": {
            "statusInfo": {"alias": "SAP_VALIDATING_SUCCESS"},
            "itemsList": [{"sku": "1", "count": 2, "countPick": 1}],
        }}}),
    )
    client = PickingClient(cfg)
    client.token = "T"
    with pytest.raises(LoApiError, match="собран не полностью"):
        _validate_sap(client, "900001")


def test_delivered_accepts_finished(cfg: Settings, monkeypatch):
    """После DELIVERED заказ B2B сразу уезжает в FINISHED — это не ошибка.

    А вот статус, который не сменился, значит, что следующий шаг не примут.
    """
    from lo_api.picking import PickingClient, deliver_order

    order = {"statusInfo": {"alias": "ASSEMBLE_COMPLETE"}, "id": "900001126144"}
    returned = {"DELIVERING": "DELIVERING", "WAITING_CLIENT": "WAITING_CLIENT",
                "DELIVERED": "FINISHED"}

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        if method == "orderGet":
            return _resp(200, {"result": {"order": order}})
        if method == "orderUpdate":
            alias = returned[json["params"]["order"]["status"]]
            return _resp(200, {"result": {"order": {"statusInfo": {"alias": alias}}}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    deliver_order(client, "900001126144")  # FINISHED принят, исключения нет

    # А теперь статус не меняется — дальше идти нельзя
    returned["DELIVERING"] = "ASSEMBLE_COMPLETE"
    with pytest.raises(LoApiError, match="не перешёл в DELIVERING"):
        deliver_order(client, "900001126144")


def test_packaging_survives_dead_packages_service(cfg: Settings, monkeypatch):
    """Недоступный getPackages не должен ронять уже собранный заказ.

    Метод только читает список типов упаковки и ничего не меняет, а сервис
    упаковки на стенде регулярно отвечает «connection refused».
    """
    from lo_api.picking import PickingClient, pick_order

    cfg.retry_backoff = 0
    order = {
        "warehouses": [{"id": 1, "storeId": 12}],
        "statusInfo": {"alias": "SAP_VALIDATING_SUCCESS"},
        "itemsList": [{"sku": "1", "name": "Кофе", "barcode": "1", "count": 1,
                       "stockData": {"records": [
                           {"count": 1, "countPick": 1, "id": 1, "storeId": 12}]}}],
    }
    calls: list[str] = []

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        calls.append(method)
        if method == "getPackages":
            return _resp(500, {"error": {"code": 1, "message":
                                         "connection error: connection refused"}})
        if method == "orderGet":
            return _resp(200, {"result": {"order": order}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    assert pick_order(client, "900001") == "assembled"
    # PACKAGING и ASSEMBLE_COMPLETE всё равно отправлены
    assert calls.count("orderUpdate") == 2


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("отказ в соединении",
         "connection error: dial tcp 10.0.0.1:5051: connect: connection refused"),
        # Сервис жив, но не может достучаться до своей базы: имени нет в кластере.
        # Ровно так выглядит падение staffWorkshiftStart_v2 на сломанном стенде.
        ("не резолвится база",
         "Внутренняя ошибка сервиса: error with CheckLearnCourseCompleted: "
         "failed to connect to `host=postgresql-picker-service user=picker-service "
         "database=picker-service`: hostname resolving error "
         "(lookup postgresql-picker-service on 169.254.25.10:53: no such host)"),
    ],
    ids=["connection-refused", "dns-no-such-host"],
)
def test_unreachable_service_says_retries_are_pointless(
    cfg: Settings, monkeypatch, case: str, message: str
):
    """Совет наращивать попытки вреден, когда сервис или его база недоступны.

    DNS-сбой внутреннего имени — такая же неустранимая повторами поломка, как
    отказ в соединении: пока стенд не починят, попытки только тратят время.
    """
    from lo_api.picker import PickerError, PickerRpc

    cfg.retry_backoff = 0
    monkeypatch.setattr(
        requests.Session, "post",
        lambda self, url, json=None, headers=None, **kw: _resp(
            500, {"error": {"message": message}}),
    )
    with pytest.raises(PickerError, match="чинится на стороне стенда") as exc:
        PickerRpc(cfg).call("getPackages", {})

    assert "LO_WORKSHIFT_RETRY_ATTEMPTS" not in str(exc.value), (
        f"{case}: нельзя советовать наращивать попытки — поломка не на нашей стороне"
    )


def test_sap_validate_repeats_until_success(cfg: Settings, monkeypatch):
    """sapValidate повторяется после каждого неуспешного чтения статуса.

    Валидация асинхронная: промежуточный SAP_VALIDATING означает «ещё идёт»,
    и коллекция снова шлёт sapValidate, а не ждёт молча. Коды 4333 «принят в
    обработку» и 3358 «статус изменён» приходят в поле error при HTTP 200 —
    это не отказ.
    """
    from lo_api.picking import PickingClient, _validate_sap

    states = ["ASSEMBLE_INCOMPLETE", "SAP_VALIDATING", "SAP_VALIDATING",
              "SAP_VALIDATING_SUCCESS"]
    calls: list[str] = []
    reads: list[int] = []

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        calls.append(method)
        if method == "orderGet":
            alias = states[min(len(reads), len(states) - 1)]
            reads.append(1)
            return _resp(200, {"result": {"order": {"statusInfo": {"alias": alias}}}})
        if method == "sapValidate":
            return _resp(200, {"error": {"code": 4333, "message": "принят в обработку"}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    _validate_sap(client, "900001")

    assert calls.count("sapValidate") == 3, "повтор на каждое неуспешное чтение"
    assert calls[-1] == "orderGet", "цикл заканчивается чтением успешного статуса"


def test_sap_validate_stops_on_error(cfg: Settings, monkeypatch):
    """SAP_VALIDATING_ERROR — дальше идти нельзя."""
    from lo_api.picking import PickingClient, _validate_sap

    monkeypatch.setattr(
        requests.Session, "post",
        lambda self, url, json=None, headers=None, **kw: _resp(200, {
            "result": {"order": {"statusInfo": {"alias": "SAP_VALIDATING_ERROR"}}}}),
    )
    client = PickingClient(cfg)
    client.token = "T"
    with pytest.raises(LoApiError, match="завершилась ошибкой"):
        _validate_sap(client, "900001")


def test_sap_validate_runs_even_for_simple_order(cfg: Settings, monkeypatch):
    """Валидация в SAP нужна любому заказу, не только партионному.

    Без неё переход в PACKAGING отвергается: «Завершение сборки по заказу
    невозможно, заказ не отправлен в SAP».
    """
    from lo_api.picking import PickingClient, pick_order

    cfg.order_poll_interval = 0
    order = {
        "warehouses": [{"id": 1, "storeId": 12}],
        "statusInfo": {"alias": "ACCEPTED"},
        "itemsList": [{"sku": "520787", "name": "Кофе", "barcode": "1", "count": 1,
                       "stockData": {"records": [
                           {"count": 1, "countPick": None, "id": 1, "storeId": 12}]}}],
    }
    calls: list[str] = []
    seen: list[int] = []

    def fake(self, url, json=None, headers=None, **kw):
        calls.append(json["method"])
        if json["method"] == "orderGet":
            seen.append(1)
            alias = {1: "ACCEPTED", 2: "ASSEMBLE_INCOMPLETE"}.get(
                len(seen), "SAP_VALIDATING_SUCCESS")
            items = [{**i, "countPick": i["count"]} for i in order["itemsList"]]
            return _resp(200, {"result": {"order": {
                **order, "itemsList": items, "statusInfo": {"alias": alias}}}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    pick_order(client, "900001")

    assert "sapValidate" in calls
    # Валидация идёт после сборки и до упаковки
    assert calls.index("sapValidate") > calls.index("itemUpdateByBarcode")
    assert calls.index("sapValidate") < calls.index("getSkuGroupsOrderBySection")


def test_resume_skips_already_picked(cfg: Settings):
    """При возобновлении берётся только недостающее количество.

    Повторная сборка собранной позиции заводит партию второй раз, и SAP
    отвечает «только одна несобранная партия может быть без ИНН».
    """
    from lo_api.picking import build_plan

    def item(sku: str, picked, **kw) -> dict:
        return {"sku": sku, "name": sku, "barcode": "1", "count": 2,
                "stockData": {"records": [
                    {"count": 2, "countPick": picked, "id": 1, "storeId": 12}]},
                **kw}

    order = {"warehouses": [{"id": 1, "storeId": 12}], "itemsList": [
        item("1", 2),                                  # собрана полностью
        item("2", 1, isMarkingInGoods=True),           # собрана наполовину
        item("3", None),                               # не начата
    ]}

    plan = build_plan(order, resume=True)
    assert [(t.sku, t.count) for t in plan.tasks] == [("2", 1), ("3", 2)]
    # Марок нужно столько, сколько осталось собрать, а не сколько заказано
    assert next(t for t in plan.tasks if t.sku == "2").marks == 1
    assert len(plan.picked) == 2

    # Без возобновления собирается всё заказанное
    assert [(t.sku, t.count) for t in build_plan(order).tasks] == [
        ("1", 2), ("2", 2), ("3", 2),
    ]


def test_fully_picked_order_goes_to_packaging(cfg: Settings, monkeypatch):
    """Собранный заказ не пересобирается, а идёт к проверке комплектации."""
    from lo_api.picking import PickingClient, pick_order

    order = {
        "warehouses": [{"id": 1, "storeId": 12}],
        "statusInfo": {"alias": "ASSEMBLE_INCOMPLETE"},
        "itemsList": [{"sku": "1", "name": "Икра", "barcode": "1", "count": 1,
                       "controlType": "V",
                       "stockData": {"records": [
                           {"count": 1, "countPick": 1, "id": 1, "storeId": 12}]}}],
    }
    calls: list[str] = []

    def fake(self, url, json=None, headers=None, **kw):
        calls.append(json["method"])
        if json["method"] == "orderGet":
            return _resp(200, {"result": {"order": {
                **order, "statusInfo": {"alias": "SAP_VALIDATING_SUCCESS"}}}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    pick_order(client, "900001")

    assert "sapBatch" not in calls, "собранная позиция не должна заводить партию заново"
    assert "itemUpdateByBarcode" not in calls
    assert calls[-1] == "orderUpdate"  # ASSEMBLE_COMPLETE


def test_batch_date_priority_chain(cfg: Settings):
    """Приоритет даты партии сверху вниз — как batchDate в flow.js.

    У sapBatchAdd и sapBatch свои переменные: партия, которую принимает одна
    ветка, другой может не подойти, и SAP отвечает «партия с указанными
    признаками не найдена».
    """
    from lo_api.picking import PickTask, _batch_date

    def task(kind: str, sku: str = "189454", batches=None) -> PickTask:
        return PickTask(sku=sku, barcode="1", count=1, stock_id=1, store_id=1,
                        kind=kind, batches=batches or [])

    from_order = [{"dateOfManufacture": "2024-01-01"}]

    cfg.batch_dates = {"189454": "2025-06-10"}
    cfg.batch_date_sap = "2025-02-02"
    cfg.batch_date = "2026-02-26"
    assert _batch_date(cfg, task("MARK_OSU_BATCH", batches=from_order)) == "2025-06-10"

    cfg.batch_dates = {}
    assert _batch_date(cfg, task("MARK_OSU_BATCH", batches=from_order)) == "2025-02-02"

    # У ветки sapBatchAdd своя переменная
    cfg.batch_date_add = "2025-03-03"
    assert _batch_date(cfg, task("MARK_PEU_BATCH", batches=from_order)) == "2025-03-03"

    cfg.batch_date_sap = cfg.batch_date_add = ""
    assert _batch_date(cfg, task("BATCH", batches=from_order)) == "2024-01-01"
    assert _batch_date(cfg, task("BATCH")) == "2026-02-26"

    # В заказе sku с ведущим нулём, в конфиге — без него
    cfg.batch_dates = {"064536": "2025-12-26"}
    assert _batch_date(cfg, task("BATCH", sku="64536")) == "2025-12-26"


def test_batch_date_is_required(cfg: Settings):
    """Без даты партии скрипт останавливается и говорит, что заполнить.

    Выдуманная дата всё равно будет отклонена методом, но выглядеть это будет
    случайным сбоем.
    """
    from lo_api.picking import PickTask, _batch_date

    cfg.batch_date = ""
    task = PickTask(sku="575098", barcode="1", count=1, stock_id=1,
                    store_id=1, kind="BATCH")
    with pytest.raises(LoApiError, match="LO_BATCH_DATE"):
        _batch_date(cfg, task)

    # Дата для конкретного товара важнее общей
    cfg.batch_dates = {"575098": "2026-02-26"}
    cfg.batch_date = "2020-01-01"
    assert _batch_date(cfg, task) == "2026-02-26"


def test_pickup_needs_no_courier_in_tms():
    """Самовывозу курьер в TMS не нужен — и не должен блокировать назначение.

    Заказ 4621169495 встал ровно на этом: сборщик и курьер вышли на смену,
    но проверка требовала настоящих в обоих списках сразу. В самой форме для
    такого случая есть «Не переносить» — пустой couriers_field_choices.
    """
    from lo_api.picking import is_pickup_id
    from lo_api.tms import OrderPage, Performer

    assert is_pickup_id("4621169495") and not is_pickup_id("900001126147")

    page = OrderPage(csrf="x", action="Сохранить",
                     pickers=[Performer("1", "Dummy"), Performer("95371423", "Пикерок")],
                     couriers=[Performer("2", "Dummy 2")])
    assert [p.id for p in page.real_pickers] == ["95371423"]
    assert page.real_couriers == []
    assert not page.has_real_performers, "для доставки этого мало"

    # В сводке для журнала видно, чего именно не хватает
    assert "настоящих 1" in page.summary() and "только заглушки" in page.summary()


def test_assign_without_courier_sends_do_not_transfer(cfg: Settings, monkeypatch):
    """Без курьера форма отправляется с «Не переносить», а не с заглушкой."""
    from lo_api.tms import OrderPage, Performer, TmsClient

    sent = {}

    def fake(self, method, url, **kw):
        sent.update(kw.get("data") or {})
        return _resp(200, {})

    monkeypatch.setattr(requests.Session, "request", fake)
    tms = TmsClient(cfg)
    page = OrderPage(csrf="c", action="Сохранить", pickers=[], couriers=[])
    tms.assign("4621169495", page, picker=Performer("95371423", "Пикерок"))

    assert sent["new_picker"] == "95371423"
    assert sent["new_couriers"] == ""
    assert sent["couriers_field_choices"] == "", "«Не переносить» из самой формы"


def test_staff_list_and_single_vars_agree():
    """Список сотрудников и одиночные переменные — две грани одного.

    Остальной код работает с picker_login/picker_password: это «текущий»
    сотрудник, которого при выводе на смену можно переключить. Прежний способ
    задать одну учётку остаётся рабочим — она становится списком из одного.
    """
    from lo_api.config import Settings, Staff

    only_single = Settings(_env_file=None, picker_login="95371423", picker_password="58923")
    assert [s.login for s in only_single.pickers] == ["95371423"]

    only_list = Settings(_env_file=None, pickers=[
        {"login": "95371423", "password": "58923", "label": "Пикерок"},
        {"login": "95371424", "password": "65035"},
    ])
    assert only_list.picker_login == "95371423", "первый в списке — текущий"
    assert only_list.picker_password == "58923"

    only_list.use_staff("picker", only_list.pickers[1])
    assert (only_list.picker_login, only_list.picker_password) == ("95371424", "65035")

    # Учётка из одиночных переменных не теряется, даже если её нет в списке
    mixed = Settings(_env_file=None, picker_login="999", picker_password="p",
                     pickers=[Staff(login="95371423", password="58923")])
    assert [s.login for s in mixed.pickers] == ["999", "95371423"]


def test_shift_choice_drives_picking_and_assignment(cfg: Settings, monkeypatch):
    """Выбранная учётка идёт и в смену, и в сборку, и в назначение TMS.

    Иначе на смену выходит один сборщик, а заказ уходит первому попавшемуся
    из списка TMS — собирать его будет некому.
    """
    import argparse

    from lo_api.cli.order import choose_staff

    cfg.pickers = [
        Staff(login="95371423", password="58923", label="Пикерок"),
        Staff(login="95371424", password="65035", label="Пикер второй"),
    ]
    args = argparse.Namespace(picker="95371424", courier=None, yes=True)

    choose_staff(cfg, "picker", "Сборщик", args.picker, args)
    assert cfg.picker_login == "95371424"
    assert cfg.picker_password == "65035", "пароль берётся от той же учётки"

    # Неизвестная учётка не уводит прогон на первую попавшуюся
    with pytest.raises(SystemExit, match="95371423"):
        choose_staff(cfg, "picker", "Сборщик", "нет-такого", args)


def test_stand_derives_hosts():
    """Стенд задаётся одним именем, хосты выводятся из него.

    Шаблон сверен с десятью окружениями коллекции: все сервисы живут по
    `<сервис>-<стенд>.dev.lenta.tech`, кроме Goods — он один на все стенды.
    Заданный явно хост важнее выведенного: в окружении Mntk сервисы взяты со
    stage, а tms — с test07, и шаблоном такое не описать.
    """
    from lo_api.config import Settings

    stage = Settings(_env_file=None, stand="stage")
    assert stage.base_url == "https://lenta-angular-stage.dev.lenta.tech"
    assert stage.picker_proxy_url == "picker-service-proxy-stage.dev.lenta.tech"
    assert stage.tms_url == "https://tms-lite-api-stage.dev.lenta.tech"
    # Goods общий для всех стендов
    assert stage.goods_url == Settings(_env_file=None, stand="test12").goods_url

    mixed = Settings(_env_file=None, stand="stage",
                     tms_url="https://tms-lite-api-test07.dev.lenta.tech")
    assert mixed.base_url == "https://lenta-angular-stage.dev.lenta.tech"
    assert mixed.tms_url == "https://tms-lite-api-test07.dev.lenta.tech"

    # Схему расставляет сам: в файле стенда все адреса пишутся одинаково
    written = Settings(_env_file=None, stand="stage",
                       base_url="lenta-angular-stage.dev.lenta.tech",
                       picker_proxy_url="https://picker-service-proxy-stage.dev.lenta.tech/",
                       tms_url="tms-lite-api-stage.dev.lenta.tech",
                       goods_url="https://goods-app-stage.dev.lenta.tech")
    for field in ("base_url", "picker_proxy_url", "tms_url", "goods_url"):
        assert getattr(written, field) == getattr(stage, field), field


def _open_order_stand(state: dict):
    """Заглушка picker-service: сборщик держит открытый заказ 900001126237.

    Пока он открыт, перевод другого заказа в ASSEMBLE_INCOMPLETE отвергается
    так же, как на стенде — HTTP 200 и ошибка в теле.
    """
    blocked = "Невозможно начать сборку нескольких заказов. Открытый заказ 900001126237"
    item = {"sku": "1", "name": "Кофе", "barcode": "4600000000017", "count": 1,
            "stockData": {"records": [{"count": 1, "countPick": None,
                                       "id": 1, "storeId": 12}]}}

    def fake(self, url, json=None, headers=None, **kw):
        method, params = json["method"], json["params"]
        state["calls"].append((method, params))
        order_id = str(params.get("orderId") or params.get("order", {}).get("id", ""))
        if method == "orderUpdate" and params["order"].get("status") == "ASSEMBLE_INCOMPLETE" \
                and order_id != "900001126237" and state["open"]:
            return _resp(200, {"error": {"code": 1, "message": blocked}})
        if method == "orderReasonsCancel":
            return _resp(200, {"result": {"reasons": [
                {"id": 7, "name": "Нет товара"}, {"id": 9, "name": "Отказ клиента"}]}})
        if method == "orderPickerCancel":
            state["open"] = False
            return _resp(200, {"result": {}})
        if method == "orderGet":
            alias = state["status"].get(order_id, "ACCEPTED")
            picked = [{**item, "countPick": 1}] if alias == "SAP_VALIDATING_SUCCESS" else [item]
            return _resp(200, {"result": {"order": {
                "id": order_id, "statusInfo": {"alias": alias},
                "warehouses": [{"id": 1, "storeId": 12}], "itemsList": picked}}})
        if method == "sapValidate":
            state["status"][order_id] = "SAP_VALIDATING_SUCCESS"
            if order_id == "900001126237":
                state["open"] = False
        return _resp(200, {"result": {"order": {"statusInfo": {"alias": "OK"}}}})

    return fake


@pytest.mark.parametrize("choice, expected", [
    ("1", "sapValidate"),        # дособрать и отправить на проверку
    ("2", "orderPickerCancel"),  # отменить
])
def test_open_order_is_resolved_then_picking_continues(cfg: Settings, monkeypatch,
                                                        choice, expected):
    """Открытый заказ сборщика не валит прогон: спросили, разобрались, продолжили."""
    from lo_api.cli.order import build_parser, pick_resolving_open_order
    from lo_api.picking import PickingClient

    state = {"calls": [], "open": True, "status": {}}
    monkeypatch.setattr(requests.Session, "post", _open_order_stand(state))
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    answers = [choice, "2"]  # действие; причина отмены — вторая в списке
    monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))

    client = PickingClient(cfg)
    client.token = "T"
    outcome = pick_resolving_open_order(client, "900001126300", build_parser().parse_args([]))
    assert outcome == "assembled"

    other = [(m, p) for m, p in state["calls"]
             if str(p.get("orderId") or p.get("order", {}).get("id")) == "900001126237"]
    assert any(m == expected for m, _ in other)
    if expected == "orderPickerCancel":
        cancel = next(p for m, p in other if m == "orderPickerCancel")
        assert cancel == {"orderId": "900001126237", "reasonId": 9}, "причина из списка"
    else:
        # Дособирается только до проверки: упаковка — уже не наше дело
        assert not any(p.get("order", {}).get("status") == "PACKAGING" for m, p in other)


def _blocked_route_stand(state: dict):
    """Заглушка: маршрут курьера держит несобранный заказ 900001126271.

    Пока тот не собран или не отменён, DELIVERING для 900001126274 получает
    ошибку 637 — дословно как в журнале прогона 29.09.
    """
    blocked = ("Нельзя начать доставку маршрута при наличии несобранных заказов "
               "(orderId: 900001126271). Обратитесь к сборщику.")
    item = {"sku": "1", "name": "Кофе", "barcode": "4600000000017", "count": 1,
            "stockData": {"records": [{"count": 1, "countPick": None,
                                       "id": 1, "storeId": 12}]}}
    staff = {"pickerStaffInfo": {"buyerId": 95371423, "name": "Пикерок"},
             "courierStaffInfo": {"buyerId": 95371418, "name": "Курьерыч"}}

    def fake(self, url, json=None, headers=None, **kw):
        method, params = json["method"], json["params"]
        state["calls"].append((method, params))
        if method == "authTokenGet":
            return _resp(200, {"result": {"token": "C"}})
        oid = str(params.get("orderId") or params.get("order", {}).get("id", ""))
        status = params.get("order", {}).get("status")
        if method == "orderUpdate" and status == "DELIVERING" and not state["free"]:
            return _resp(200, {"error": {"code": 637, "message": blocked}})
        if method == "orderUpdate" and status:
            state["status"][oid] = status
            if oid == "900001126271" and status == "ASSEMBLE_COMPLETE":
                state["free"] = True
            return _resp(200, {"result": {"order": {"statusInfo": {"alias": status}}}})
        if method == "orderPickerCancel":
            state["free"] = True
            return _resp(200, {"result": {}})
        if method == "orderReasonsCancel":
            return _resp(200, {"result": {"reasons": [{"id": 7, "name": "Нет товара"}]}})
        if method == "sapValidate":
            state["status"][oid] = "SAP_VALIDATING_SUCCESS"
        if method == "orderGet":
            alias = state["status"].get(oid, "ACCEPTED")
            picked = [{**item, "countPick": 1}] if alias != "ACCEPTED" else [item]
            return _resp(200, {"result": {"order": {
                "id": oid, "statusInfo": {"alias": alias}, **staff,
                "warehouses": [{"id": 1, "storeId": 12}], "itemsList": picked}}})
        return _resp(200, {"result": {"order": {"statusInfo": {"alias": "OK"}}}})

    return fake


@pytest.mark.parametrize("choice, expected", [("1", "ASSEMBLE_COMPLETE"),
                                              ("2", "orderPickerCancel")])
def test_route_blocker_is_resolved_then_delivery_continues(cfg: Settings, monkeypatch,
                                                           choice, expected):
    """Собранный заказ не застревает в ASSEMBLE_COMPLETE из-за чужого в маршруте."""
    from lo_api.cli.order import build_parser, deliver_resolving_route
    from lo_api.picking import PickingClient

    state = {"calls": [], "free": False,
             "status": {"900001126274": "ASSEMBLE_COMPLETE"}}
    monkeypatch.setattr(requests.Session, "post", _blocked_route_stand(state))
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    answers = [choice]
    monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))
    cfg.courier_login, cfg.courier_password = "95371418", "p"

    picker = PickingClient(cfg)
    picker.token = "T"
    deliver_resolving_route(cfg, picker, "900001126274", build_parser().parse_args([]))

    assert state["status"]["900001126274"] in ("DELIVERED", "FINISHED")
    if expected == "orderPickerCancel":
        assert ("orderPickerCancel", {"orderId": "900001126271", "reasonId": 7}) \
            in state["calls"]
    else:
        assert state["status"]["900001126271"] == "ASSEMBLE_COMPLETE", "дособран до конца"


def test_open_order_needs_consent_without_dialog(cfg: Settings, monkeypatch):
    """Без диалога чужой заказ не трогается: нужен явный --open-order."""
    from lo_api.cli.order import build_parser, pick_resolving_open_order
    from lo_api.picking import PickingClient

    state = {"calls": [], "open": True, "status": {}}
    monkeypatch.setattr(requests.Session, "post", _open_order_stand(state))
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: False)
    client = PickingClient(cfg)
    client.token = "T"

    with pytest.raises(SystemExit, match="--open-order"):
        pick_resolving_open_order(client, "900001126300", build_parser().parse_args([]))
    assert not any(m in ("orderPickerCancel", "sapValidate") for m, _ in state["calls"])

    args = build_parser().parse_args(["--open-order", "cancel"])
    assert pick_resolving_open_order(client, "900001126300", args) == "assembled"


def test_packages_down_is_reported_as_expected(cfg: Settings, monkeypatch, caplog):
    """getPackages лежит на стенде всегда — и сообщение не зовёт ждать починки.

    Общий совет picker-service («дождитесь, пока поднимут») рядом с «продолжаю
    без него» противоречил сам себе. Ответ стенда уходит только в журнал.
    """
    import logging

    from lo_api.picker import tries
    from lo_api.picking import PickingClient, pick_order

    assert [tries(n) for n in (1, 3, 5, 11, 21, 22)] == [
        "за 1 попытку", "за 3 попытки", "за 5 попыток", "за 11 попыток",
        "за 21 попытку", "за 22 попытки"]

    down = ('connection error: desc = "transport: Error while dialing: dial tcp '
            '10.249.28.49:5051: connect: connection refused"')

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        if method == "getPackages":
            return _resp(500, {"error": {"message": down}})
        alias = "SAP_VALIDATING_SUCCESS" if method == "orderGet" else "ASSEMBLE_COMPLETE"
        return _resp(200, {"result": {"order": {"statusInfo": {"alias": alias},
                                                "itemsList": []}}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    with caplog.at_level(logging.DEBUG, logger="lo_api.picking"):
        assert pick_order(client, "900001") == "assembled"

    warning = next(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "сервис упаковки на стенде не поднят, это ожидаемо" in warning
    assert "дождитесь" not in warning and "попытк" not in warning
    assert any(down in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_short_mark_hint_uses_stand_text(cfg: Settings, monkeypatch):
    """Подсказка про группу марки строится, а не падает на AttributeError.

    Код обращался к exc.error, которого у PickerError нет: вместо подсказки
    «укажите LO_MARKING_TYPES» прогон рушился трейсбеком.
    """
    from lo_api.picker import PickerError
    from lo_api.picking import PickingClient, PickTask, _do_marks

    def fake(self, url, json=None, headers=None, **kw):
        return _resp(200, {"error": {"code": 650,
                                     "message": "Не удалось получить короткую марку"}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    task = PickTask(sku="573704", barcode="4600000000017", count=1, stock_id=1,
                    store_id=12, kind="MARK_PEU", name="Вода", marks=1)
    with pytest.raises(PickerError) as caught:
        _do_marks(client, "900001", task, batch=False)
    text = str(caught.value)
    assert text.count("Не удалось получить короткую марку") == 1
    assert 'LO_MARKING_TYPES={"573704": "<группа>"}' in text


def test_shipped_stand_files_load():
    """Файлы стендов из репозитория читаются, и образец тоже.

    Пустое значение у списка, словаря или числа pydantic не разбирает, так что
    образец с одними пустыми строками не завёлся бы: у таких переменных должны
    стоять [] и {}, а необязательные числовые — закомментированы.
    """
    from lo_api.config import ENV_DIR, Settings, available_stands

    known = available_stands()
    assert "test12" in known and "operation" in known

    for stand in known:
        cfg = Settings(_env_file=[f"{ENV_DIR}/{stand}.env"], stand=stand)
        # Адреса записаны в файле голыми именами, а сходятся с выводимыми
        assert cfg.base_url == f"https://lenta-angular-{stand}.dev.lenta.tech"
        assert cfg.picker_proxy_url == f"picker-service-proxy-{stand}.dev.lenta.tech"
        assert cfg.tms_url == f"https://tms-lite-api-{stand}.dev.lenta.tech"
        assert cfg.goods_url == "goods-app-stage.dev.lenta.tech"
        assert cfg.pickers and cfg.couriers, f"{stand}: нет учёток сотрудников"
        assert cfg.mp_picker_key, f"{stand}: нет ключа маркетингового партнёра"

    sample = Settings(_env_file=[f"{ENV_DIR}/stand.env.example"], stand="probe")
    assert sample.goods_store_id, "образец должен заводиться как есть"


def test_stand_file_and_personal_env(tmp_path, monkeypatch):
    """Личный .env накладывается поверх общего файла стенда.

    В env/<стенд>.env лежит общее — учётки сотрудников, магазин; в .env
    личное — свой номер и токен. Если значение есть в обоих, побеждает личное,
    иначе чужой файл переопределял бы аккаунт, под которым идёт прогон.
    """
    from lo_api.config import available_stands, load_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LO_STAND", raising=False)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "stage.env").write_text(
        "LO_PICKER_LOGIN=111\nLO_WORKSHIFT_TK=7\n", encoding="utf-8")
    (tmp_path / "env" / "test12.env").write_text("LO_PICKER_LOGIN=222\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LO_STAND=stage\nLO_PICKER_LOGIN=999\n", encoding="utf-8")

    assert available_stands() == ["stage", "test12"]

    cfg = load_settings()
    assert cfg.stand == "stage"
    assert cfg.workshift_tk == "7", "значение из файла стенда"
    assert cfg.picker_login == "999", "личное важнее общего"
    assert cfg.base_url == "https://lenta-angular-stage.dev.lenta.tech"

    # Явный выбор перебивает LO_STAND из .env
    assert load_settings("test12").workshift_tk != "7"


def test_unknown_stand_names_the_known_ones(tmp_path, monkeypatch):
    """Опечатка в названии стенда не должна молча уводить на умолчания."""
    from lo_api.config import load_settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "test12.env").write_text("LO_WORKSHIFT_TK=43\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="test12"):
        load_settings("test07")

    # Папки стендов нет вовсе — работает прежний способ с одним .env
    monkeypatch.chdir(tmp_path / "env")
    assert load_settings("test07").stand == "test07"


def test_log_accumulates_runs(tmp_path, monkeypatch):
    """Лог заказа копит прогоны, а не начинается заново с каждым запуском.

    Один заказ доводят в несколько заходов — дособрать, довалидировать,
    доставить. Переименование run-*.log в «<номер>.log» затирало предыдущий
    файл, и история прошлых заходов пропадала.
    """
    import logging
    import sys

    from lo_api.cli.order import name_log_after_order, setup_logging

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lo-order", "--token", "СЕКРЕТ", "--code=1234"])

    for run in (1, 2):
        setup_logging(verbose=False, log_file="", log_dir="logs")
        logging.getLogger("lo_api.picking").info("прогон %s", run)
        name_log_after_order("900001126147")
    logging.getLogger().handlers.clear()

    written = (tmp_path / "logs" / "900001126147.log").read_text(encoding="utf-8")
    assert "прогон 1" in written and "прогон 2" in written
    # Временный файл прогона не остаётся рядом с итоговым
    assert [p.name for p in (tmp_path / "logs").iterdir()] == ["900001126147.log"]
    # Секреты из командной строки в заголовок не попадают
    assert "СЕКРЕТ" not in written and "1234" not in written
    assert written.count("# прогон") == 2


def test_assignment_seen_in_order():
    """Назначение видно в самом заказе, гонять ради него TMS не нужно.

    Формы взяты из логов прогонов: у неназначенного заказа обе структуры
    `null`, у назначенного на заглушку приходит имя «Dummy», а свободное место
    сборщика стенд помечает buyerId = INT_MAX с пустым именем.
    """
    from lo_api.picking import assigned_performers, assignment_issues

    real_picker = {"buyerId": 95371423, "groupId": 83, "name": "Пикерок БиТуБи"}
    real_courier = {"buyerId": 95371418, "groupId": 82, "name": "Курьерыч БиТуБи"}

    assert assignment_issues({"pickerStaffInfo": None, "courierStaffInfo": None}) == [
        "сборщик", "курьер"]
    assert assignment_issues({
        "pickerStaffInfo": {"buyerId": 2147483647, "name": None},
        "courierStaffInfo": {"buyerId": 435466261916, "name": "Dummy"},
    }) == ["сборщик", "курьер"]
    assert assignment_issues({"pickerStaffInfo": real_picker,
                              "courierStaffInfo": real_courier}) == []

    # Самовывоз курьеру не отдаётся: требовать его — значит переназначать
    # такой заказ перед каждой сборкой впустую
    pickup = {"id": "4621169458", "pickerStaffInfo": real_picker, "courierStaffInfo": None}
    assert assignment_issues(pickup) == []

    assert assigned_performers({"pickerStaffInfo": real_picker,
                                "courierStaffInfo": {"name": "Dummy", "buyerId": 1}}) == {
        "сборщик": "Пикерок БиТуБи"}


def test_dns_failure_blames_vpn():
    """Нерезолвящееся имя хоста — это отключённый VPN, и так и надо сказать.

    Стенды живут во внутренней сети. Голый текст исключения requests выглядит
    как поломка скрипта, хотя чинится подключением к VPN. Имя хоста в подсказке
    без пути эндпоинта: иначе она тонет в URL.
    """
    from lo_api.http.errors import network_error_text

    text = network_error_text(
        requests.ConnectionError(
            "HTTPSConnectionPool(host='picker-service-proxy-test12.dev.lenta.tech', "
            "port=443): Max retries exceeded (Caused by NameResolutionError(...))"
        ),
        "https://picker-service-proxy-test12.dev.lenta.tech/jrpc",
    )
    assert "VPN" in text
    assert "picker-service-proxy-test12.dev.lenta.tech" in text
    assert "/jrpc" not in text

    # Обычный обрыв связи VPN не касается: подсказка была бы враньём
    other = network_error_text(requests.ConnectionError("Connection reset by peer"), "host")
    assert "VPN" not in other and "Connection reset by peer" in other


def test_own_needs_needs_both_flags():
    """Собственные нужды — это markOwnNeeds И vetOwnNeeds разом.

    proInfo лежит на разной глубине и приходит не в orderGet, а в ответах
    orderUpdate и сканов, поэтому ищется рекурсивно. Отсутствие флагов — не
    «нет собственных нужд», а «в этом ответе не сказано».
    """
    from lo_api.picking import own_needs_from

    deep = {"data": {"task": {"orders": [{"proInfo": {
        "markOwnNeeds": True, "vetOwnNeeds": True}}]}}}
    assert own_needs_from(deep) is True
    assert own_needs_from({"order": {"proInfo": {
        "markOwnNeeds": True, "vetOwnNeeds": False}}}) is False
    # proInfo без флагов — как в первом orderGet
    assert own_needs_from({"order": {"proInfo": {"vetCertificate": "Нужны"}}}) is None
    assert own_needs_from({"order": {}}) is None


def test_own_needs_skips_batches(cfg: Settings, monkeypatch):
    """В собственные нужды партии SAP не запрашиваются.

    OWN_ROUTE меняет маршрут у всех трёх партионных типов: BATCH идёт только
    количеством, MARK_PEU_BATCH — сразу марками без sapBatchAdd, а
    MARK_OSU_BATCH повторяет обычный ОСУ (одна марка, дальше @osuQuantity).
    Марки при этом снимаются как обычно.
    """
    from lo_api.picking import PickingClient, pick_order

    cfg.batch_date = "2026-02-26"
    order = {"warehouses": [{"id": 1, "storeId": 12}],
             "statusInfo": {"alias": "ACCEPTED"}, "itemsList": [
        {"sku": "1", "name": "Корм", "barcode": "1", "count": 1, "controlType": "V",
         "stockData": {"records": [{"count": 1, "countPick": None,
                                    "id": 1, "storeId": 12}]}},
        {"sku": "2", "name": "Икра", "barcode": "2", "count": 1, "controlType": "V",
         "isMarkingInGoods": True,
         "stockData": {"records": [{"count": 1, "countPick": None,
                                    "id": 1, "storeId": 12}]}},
        {"sku": "3", "name": "Вода", "barcode": "3", "count": 2, "controlType": "V",
         "isMarkingInGoods": True, "isOsuMarkRequired": True,
         "stockData": {"records": [{"count": 2, "countPick": None,
                                    "id": 1, "storeId": 12}]}},
    ]}

    def run(own: bool) -> list[str]:
        calls: list[str] = []
        reads: list[int] = []
        pro = {"markOwnNeeds": own, "vetOwnNeeds": own}

        def fake(self, url, json=None, headers=None, **kw):
            method = json["method"]
            calls.append(method)
            if method == "orderGet":
                reads.append(1)
                alias = "ACCEPTED" if len(reads) == 1 else "SAP_VALIDATING_SUCCESS"
                items = [{**i, "countPick": i["count"]} for i in order["itemsList"]]
                return _resp(200, {"result": {"order": {
                    **order, "itemsList": items,
                    "statusInfo": {"alias": alias}, "proInfo": pro}}})
            if method == "sapManufacturer":
                return _resp(200, {"result": {"manufacturers": [
                    {"code": "PP", "name": "ПП для Ленты"}]}})
            return _resp(200, {"result": {"order": {"proInfo": pro}}})

        monkeypatch.setattr(requests.Session, "post", fake)
        client = PickingClient(cfg)
        client.token = "T"
        pick_order(client, "900001")
        return calls

    usual = run(own=False)
    # Партии заводятся у BATCH и MARK_OSU_BATCH (sapBatch), у MARK_PEU_BATCH —
    # своим методом sapBatchAdd
    assert usual.count("sapBatch") == 2 and usual.count("sapBatchAdd") == 1

    own = run(own=True)
    assert own.count("sapManufacturer") == 0, "производитель партии не нужен"
    assert own.count("sapBatch") == 0 and own.count("sapBatchAdd") == 0
    # Марки снимаются в обоих случаях, но каждый тип — своим методом:
    # ПЭУ партионный шлёт itemBatchIncreaseByMarking, ОСУ — sapMarking.
    # Подтверждено записью сборки: sku 189454 (isOsuMarkRequired=true) → sapMarking
    assert own.count("itemBatchIncreaseByMarking") == 1
    assert own.count("sapMarking") == 1 == usual.count("sapMarking")
    assert own.count("itemIncreaseByMarking") == 0, (
        "ОСУ больше не уходит общим методом — марка не доезжала"
    )


def _reorder_fixture() -> dict:
    """Ответы стенда из HAR «Повтор заказа» — кофе и весовое мясо (3 × 700 г)."""
    from pathlib import Path

    return json.loads((Path(__file__).parent / "data" / "reorder.json")
                      .read_text(encoding="utf-8"))


def test_past_order_parsed_from_stand():
    """orderLookup даёт всё для повтора, весовой — в порциях, а не килограммах.

    У весового `Quantity` = 2.1 (кг), а `QuantityOrigin` = 3 — порции по 700 г.
    В корзине после cartFromOrderAdd лежат именно 3: сверять надо порции.
    """
    from lo_api.flows import reorder_issues
    from lo_api.models import Cart, PastOrder

    data = _reorder_fixture()
    past = PastOrder.model_validate(data["orderLookup"]["OrderList"][0])
    assert (past.id, past.delivery_type, past.address_id, past.payment_type_id) == \
        ("900001126273", "delivery", 15046759, 920)
    assert (past.vet_own_needs, past.mark_own_needs) == (False, True)
    meat = next(i for i in past.items if i.is_weight)
    assert (meat.sku, meat.quantity, meat.units) == ("015888", 2.1, 3)

    cart = Cart.from_body(data["cartLookup_after"])
    assert reorder_issues(past, cart) == [], "корзина стенда совпала с заказом"

    # Недоложенный весовой и лишняя позиция — видны, а не проходят молча
    cart.goods[1].quantity = 2
    assert reorder_issues(past, cart) == [
        "015888 Котлетное мясо из говядины ЛЕНТА FRESH бескостное: в корзине 2, в заказе 3"]


def test_reorder_rebuilds_order_for_today(cfg: Settings, monkeypatch):
    """Повтор по номеру: корзина от стенда, ответы из заказа, интервал — сегодня.

    Тело cartFromOrderAdd сверяется с тем, что шлёт браузер. Корзина перед
    повтором чистится, иначе остатки прошлого прогона попали бы в заказ.
    """
    from datetime import date

    from lo_api.cli.order import apply_reorder, build_params, build_parser, choose_interval
    from lo_api.flows import CheckoutFlow
    from lo_api.models import PastOrder

    data = _reorder_fixture()
    sent: list[tuple[str, dict]] = []

    def fake(self, method, url, **kw):
        body = kw.get("data") or {}
        raw = body.get("request") if isinstance(body, dict) else \
            urllib.parse.parse_qs(body).get("request", [None])[0]
        payload = json.loads(raw) if raw else {}
        name = url.rsplit("/", 1)[-1]
        sent.append((name, payload.get("Body", {})))
        if name == "cartLookup" and any(n == "cartFromOrderAdd" for n, _ in sent):
            return _resp(200, {"Head": {"Status": "success"}, "Body": data["cartLookup_after"]})
        if name == "cartLookup":
            return _resp(200, {"Head": {"Status": "success"}, "Body": {"CartList": [
                {"Goods": [{"GoodsItemId": "433217", "Quantity": 5}]}]}})
        return _resp(200, {"Head": {"Status": "success"}, "Body": {"CartList": [{}]}})

    monkeypatch.setattr(requests.Session, "request", fake)
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    past = PastOrder.model_validate(data["orderLookup"]["OrderList"][0])

    args = build_parser().parse_args([])
    apply_reorder(args, past)
    assert (args.mission, args.address_id, args.payment) == ("delivery", 15046759, "920")
    assert (args.vet_own_needs, args.mark_own_needs) == (False, True)

    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    params = build_params(api, cfg, args)          # ни одного вопроса: input не задан
    assert params.items == [("520787", 1.0), ("015888", 3.0)]

    cart, issues = CheckoutFlow(api).cart_from_order(past)
    assert cart.quantity_by_sku == {"520787": 1.0, "015888": 3.0}
    assert issues == []
    names = [n for n, _ in sent]
    assert "cartItemModify" in names[:names.index("cartFromOrderAdd")], "старая корзина очищена"
    assert dict(sent[names.index("cartFromOrderAdd")][1]) == data["cartFromOrderAdd_request"]

    today = date.today().isoformat()
    slots = [Interval(Id=1, Date="2099-01-01", TimeFrom="12:00:00"),
             Interval(Id=2, Date=today, TimeFrom="10:30:00"),
             Interval(Id=3, Date=today, TimeFrom="12:00:00")]
    assert choose_interval(slots, args).id == 3, "сегодня и то же время, что в заказе"


def test_repeat_fills_answers_and_keeps_explicit_flags(tmp_path, monkeypatch, capsys):
    """Повтор подставляет ответы прошлого заказа, но явный флаг важнее.

    Сохраняются ответы, а не заказ: при повторе они попадают туда же, куда и
    флаги, поэтому флоу оформления тот же, только без вопросов. Интервал —
    временем, а не Id: вчерашний слот уже недоступен.
    """
    from lo_api import last_order
    from lo_api.cli.order import apply_repeat, build_parser, choose_interval

    path = tmp_path / "last.json"
    last_order.save(path, last_order.LastOrder(
        stand="test12", phone="79990001122", org_inn="7806187702",
        org_name="ООО ОДИН", mission="pickup", items=[("520787", 3.0)],
        payment="702", store_id=3429, vet_own_needs=True, mark_own_needs=False,
        time_from="19:30", order_id="4621169495"))
    last = last_order.load(path)
    assert last.items == [("520787", 3.0)], "кортежи переживают JSON"
    assert last.summary() == "самовывоз, магазин 3429; 520787×3; ООО ОДИН"

    args = build_parser().parse_args(["--sku", "433217:5"])
    apply_repeat(args, last)
    assert (args.phone, args.org, args.mission, args.store_id, args.payment) == \
        ("79990001122", "7806187702", "pickup", 3429, "702")
    assert args.sku == ["433217:5"], "явный --sku перекрывает сохранённый состав"
    assert (args.vet_own_needs, args.mark_own_needs) == (True, False)

    # Интервал: то же время в ближайший день, где оно есть, — без вопросов
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    slots = [Interval(Id=1, Date="2026-09-22", TimeFrom="19:30", Disabled=True),
             Interval(Id=2, Date="2026-09-22", TimeFrom="20:00"),
             Interval(Id=3, Date="2026-09-23", TimeFrom="19:30")]
    assert choose_interval(slots, args).id == 3
    args.repeat_time = "07:00"
    assert choose_interval(slots, args).id == 2, "нет такого времени — ближайший"


def test_broken_last_order_file_is_not_fatal(tmp_path):
    """Нечитаемый файл повтора не роняет прогон — вопросы зададутся как обычно."""
    from lo_api import last_order

    path = tmp_path / "last.json"
    path.write_text("{не json", encoding="utf-8")
    assert last_order.load(path) is None
    assert last_order.load(tmp_path / "нет.json") is None


def _goods_water() -> dict:
    """Ответ Goods на воду НАРЗАН (sku 1429) — markType WATER, коробка в штрихкодах."""
    from pathlib import Path

    return json.loads((Path(__file__).parent / "data" / "goods_water.json")
                      .read_text(encoding="utf-8"))


@pytest.mark.goods
def test_marking_kind_comes_from_goods(cfg: Settings, monkeypatch, caplog):
    """Группа марки — из markType товара в Goods, а не общая на стенд.

    Заказ 900001126286: вода собиралась марками SEAFOOD, потому что в orderGet
    типа маркировки нет, и бралась общая LO_MARKING_TYPE. Goods отдаёт его
    прямо. Ручная строка LO_MARKING_TYPES по-прежнему важнее.
    """
    import logging

    from lo_api.picking import PickingClient, PickTask, _marking_kind

    asked = []

    def fake_get(self, url, **kw):
        asked.append(url)
        return _resp(200, _goods_water())

    monkeypatch.setattr(requests.Session, "get", fake_get)
    cfg.marking_type = "SEAFOOD"
    client = PickingClient(cfg)
    water = PickTask(sku="1429", barcode="4600536001012", count=10, stock_id=1,
                     store_id=12, kind="MARK_PEU", name="Вода НАРЗАН", marks=10)

    with caplog.at_level(logging.INFO):
        assert _marking_kind(client, water) == ("water", "Goods, markType WATER")
        assert _marking_kind(client, water)[0] == "water"
    assert len(asked) == 1, "карточка запрашивается один раз на SKU, а не на каждую марку"
    assert asked[0].endswith("/v2/gate/item/CHANNEL_B2B/12/1429")

    cfg.marking_types = {"001429": "construction"}
    assert _marking_kind(client, water) == ("construction", "LO_MARKING_TYPES")


def test_goods_card_fields_and_piece_barcodes():
    """В карточке Goods — группа марки, признак алкоголя и штрихкоды с единицей.

    Коробочный GTIN-14 (uom KAR) на марку не идёт, главный штучный — первым.
    """
    from lo_api.goods import find_barcodes, find_card, piece_barcodes
    from lo_api.marking import kind_from_goods

    card = find_card(_goods_water())
    assert (card["markType"], card["isAlcohol"]) == ("WATER", False)
    assert kind_from_goods("WATER") == "water"
    assert kind_from_goods("VEGETABOIL") == "vegetableoil_short", "в Goods без «LE»"
    assert kind_from_goods("") is None and kind_from_goods("НЕИЗВЕСТНО") is None

    codes = piece_barcodes(find_barcodes(card))
    assert codes[0] == "4600536001012", "главный — первым"
    assert "14600536001019" not in codes, "коробка — мимо"


def test_alcohol_flag_from_goods_beats_marking_group(cfg: Settings, monkeypatch):
    """isAlcohol из Goods решает сам, группа марки — только запасной путь."""
    from lo_api.picking import PickingClient, PickTask, is_alcohol

    task = PickTask(sku="700003", barcode="4600000000017", count=1, stock_id=1,
                    store_id=12, kind="BATCH", name="Вино")
    client = PickingClient(cfg)
    monkeypatch.setattr(PickingClient, "goods_card", lambda self, t: {"isAlcohol": True})
    assert is_alcohol(client, task), "немаркированное вино — тоже алкоголь"
    monkeypatch.setattr(PickingClient, "goods_card", lambda self, t: {"isAlcohol": False})
    cfg.marking_types = {"700003": "beer"}
    task.kind = "MARK_PEU_BATCH"
    assert not is_alcohol(client, task), "Goods важнее группы марки"


def test_own_needs_with_vsd_keeps_batches(cfg: Settings):
    """В СН партии пропускаются только без ВСД (эталон, раздел 8.7).

    Признак ВСД приходит по-разному: vetCertificate строкой в orderGet,
    hasVetCertificate в ответах сканов, vetCertificateRequired — так в эталоне.
    """
    from lo_api.picking import PickingClient, PickTask, skips_batches, vsd_required_from

    def pro(**kw):
        return {"result": {"order": {"proInfo": kw}}}

    assert vsd_required_from(pro(vetCertificate="Нужны")) is True
    assert vsd_required_from(pro(vetCertificate="Не нужны")) is False
    assert vsd_required_from(pro(hasVetCertificate=True)) is True
    assert vsd_required_from(pro(vetCertificateRequired=False, vetCertificate="Нужны")) \
        is False, "явный признак эталона важнее строки"
    assert vsd_required_from(pro(companyInn="1")) is None

    client = PickingClient(cfg)
    caviar = PickTask(sku="575098", barcode="4600000000017", count=1, stock_id=1,
                      store_id=12, kind="MARK_PEU_BATCH", name="Икра")
    client.own_needs = True
    client.tracks_assembly_flags = True  # как внутри pick_order

    client.remember_own_needs(pro(vetCertificate="Нужны"))
    assert not skips_batches(client, caviar), "СН с ВСД — партия и марки"
    client.remember_own_needs(pro(vetCertificate="Не нужны"))
    assert skips_batches(client, caviar), "СН без ВСД — только марки"


def test_sap_error_is_fixed_and_resent(cfg: Settings, monkeypatch):
    """SAP_VALIDATING_ERROR не останавливает сборку (эталон, раздел 10.3).

    Проблемная марка очищается стендом — у позиции падает countPick. Скрипт
    пересобирает недостающее новой маркой и отправляет заказ на проверку
    снова. Ошибка без проблемных позиций — просто повторная отправка целиком.
    """
    from lo_api.picking import PickingClient, pick_order

    water = {"sku": "1429", "name": "Вода", "barcode": "4600536001012", "count": 2,
             "isMarking": True, "isMarkingInGoods": True,
             "stockData": {"records": [{"count": 2, "countPick": None,
                                        "id": 1, "storeId": 12}]}}
    state = {"calls": [], "validations": 0, "picked": 0}

    def fake(self, url, json=None, headers=None, **kw):
        method = json["method"]
        state["calls"].append(method)
        if method == "itemIncreaseByMarking":
            state["picked"] += 1
        if method == "sapValidate":
            state["validations"] += 1
        if method == "orderGet":
            # 1-я отправка: одна из двух марок признана проблемной и очищена;
            # 2-я: успех
            if state["validations"] == 0:
                alias, picked = "ACCEPTED", None
            elif state["validations"] == 1:
                alias, picked = "SAP_VALIDATING_ERROR", 1
            else:
                alias, picked = "SAP_VALIDATING_SUCCESS", 2
            item = {**water, "countPick": picked,
                    "stockData": {"records": [{"count": 2, "countPick": picked,
                                               "id": 1, "storeId": 12}]}}
            return _resp(200, {"result": {"order": {
                "statusInfo": {"alias": alias, "text": "Ошибка валидации SAP"},
                "warehouses": [{"id": 1, "storeId": 12}], "itemsList": [item]}}})
        return _resp(200, {"result": {"order": {"statusInfo": {"alias": "OK"}}}})

    monkeypatch.setattr(requests.Session, "post", fake)
    client = PickingClient(cfg)
    client.token = "T"
    assert pick_order(client, "900001") == "assembled"

    assert state["validations"] == 2, "после исправления — повторная отправка"
    assert state["picked"] == 3, "две марки, затем одна взамен очищенной"


def test_sap_error_gives_up_after_retries(cfg: Settings, monkeypatch):
    """Ошибка, которую не лечит пересборка, останавливает прогон с объяснением."""
    from lo_api.picker import PickerError
    from lo_api.picking import PickingClient, pick_order

    def fake(self, url, json=None, headers=None, **kw):
        if json["method"] == "orderGet":
            return _resp(200, {"result": {"order": {
                "statusInfo": {"alias": "SAP_VALIDATING_ERROR",
                               "text": "Ошибка валидации SAP"},
                "itemsList": []}}})
        return _resp(200, {"result": {}})

    monkeypatch.setattr(requests.Session, "post", fake)
    cfg.sap_error_retries = 2
    client = PickingClient(cfg)
    client.token = "T"
    with pytest.raises(PickerError, match="за 2 попытки"):
        pick_order(client, "900001")


def test_alcohol_keeps_batches_in_own_needs(cfg: Settings):
    """У алкоголя партии запрашиваются всегда — и в собственные нужды тоже.

    OWN_ROUTE коллекции снимает партии у всех партионных типов; для пива это
    ошибка. Признака алкоголя в orderGet нет, поэтому он берётся из группы
    марки: пиву её задают всё равно, иначе марка не сгенерируется.
    """
    from lo_api.picking import PickingClient, PickTask, skips_batches

    client = PickingClient(cfg)
    client.own_needs = True
    cfg.marking_type = "milk"
    cfg.marking_types = {"700001": "beer", "700002": "nabeer"}

    def task(sku: str, kind: str = "MARK_PEU_BATCH") -> PickTask:
        return PickTask(sku=sku, barcode="4600000000017", count=1, stock_id=1,
                        store_id=12, kind=kind, name=sku)

    assert not skips_batches(client, task("700001")), "пиво — партии нужны"
    assert skips_batches(client, task("700002")), "безалкогольное — как всё остальное"
    assert skips_batches(client, task("575098")), "икра в собственные нужды — без партий"
    assert skips_batches(client, task("064536", "BATCH")), "немаркированное — без партий"

    client.own_needs = False
    assert not skips_batches(client, task("575098")), "не собственные нужды — партии нужны"


def test_goods_finds_weight_barcode():
    """Весовой ШК ищется по типу, а не по позиции в ответе.

    Ответ Goods меняет форму, поэтому ищется массив объектов с code и type.
    Из нескольких весовых кодов маркированному нужен nonmark: false.
    """
    from lo_api.goods import find_barcodes, pick_weight_barcode

    payload = {"item": {"data": {"barcodes": [
        {"code": "4600000000017", "type": "EAN13"},
        {"code": "2400138000000", "type": "Z2", "nonmark": True},
        {"code": "2900138000000", "type": "Z2", "nonmark": False},
    ]}}}
    barcodes = find_barcodes(payload)
    assert len(barcodes) == 3
    assert pick_weight_barcode(barcodes, marked=False) == "2400138000000"
    assert pick_weight_barcode(barcodes, marked=True) == "2900138000000"
    # Обычный EAN весовому не подходит
    assert pick_weight_barcode([{"code": "4600000000017", "type": "EAN13"}],
                               marked=False) is None


def test_weight_and_sample_bodies(cfg: Settings, monkeypatch):
    """Весовой сканируется ШК с граммовкой, проба — не сканируется вовсе."""
    from lo_api.picking import PickingClient, pick_order

    order = {"warehouses": [{"id": 1, "storeId": 12}],
             "statusInfo": {"alias": "ACCEPTED"}, "itemsList": [
        {"sku": "181253", "name": "Пирог", "barcode": "4600000000017",
         "unit": "GW", "count": 900, "portion": 1,
         "stockData": {"records": [{"count": 900, "countPick": None,
                                    "id": 1, "storeId": 12}]}},
        {"sku": "999", "name": "Проба", "sampleId": 7, "count": 2,
         "stockData": {"records": [{"count": 2, "countPick": None,
                                    "id": 1, "storeId": 12}]}},
    ]}
    sent: list[tuple[str, dict]] = []
    reads: list[int] = []

    def fake_post(self, url, json=None, headers=None, **kw):
        sent.append((json["method"], json["params"]))
        if json["method"] == "orderGet":
            reads.append(1)
            alias = "ACCEPTED" if len(reads) == 1 else "SAP_VALIDATING_SUCCESS"
            items = [{**i, "countPick": i["count"]} for i in order["itemsList"]]
            return _resp(200, {"result": {"order": {
                **order, "itemsList": items, "statusInfo": {"alias": alias}}}})
        return _resp(200, {"result": {}})

    def fake_get(self, url, **kw):
        return _resp(200, {"item": {"barcodes": [
            {"code": "2400138000000", "type": "Z2", "nonmark": True}]}})

    monkeypatch.setattr(requests.Session, "post", fake_post)
    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = PickingClient(cfg)
    client.token = "T"
    pick_order(client, "900001")

    scan = next(p for m, p in sent if m == "itemUpdateByBarcode")
    # Первые 7 цифр базового кода, 900 г и контрольная цифра
    assert scan["barcode"] == "2400138009001"

    updates = [p["order"]["items"][0] for m, p in sent
               if m == "orderUpdate" and "items" in p.get("order", {})]

    # Весовому количество проставляется в граммах, а не числом порций:
    # stock_count меряется в тех же единицах, что count позиции
    weight = next(u for u in updates if u.get("sku") == "181253")
    assert weight == {"sku": "181253", "stock_id": 1, "stock_count": 900}

    sample = next(u for u in updates if u.get("sku") == "999")
    assert sample == {"count": 2, "sku": "999", "sampleId": 7}


def test_weight_item_counts_units_not_grams():
    """У весового count — граммы, а число единиц лежит в portion.

    Марки и orderUpdate считаются по единицам: иначе на 900 г товара
    сгенерировалось бы 900 марок.
    """
    from lo_api.picking import build_plan

    plan = build_plan({
        "warehouses": [{"id": 1, "storeId": 12}],
        "itemsList": [{"sku": "181253", "name": "Пирог", "barcode": "2400138000000",
                       "unit": "GW", "count": 900, "portion": 1,
                       "isMarkingInGoods": True}],
    })
    task = plan.tasks[0]
    assert task.weight is True
    assert task.count == 900       # граммы — идут в штрихкод
    assert task.units == 1         # единиц одна
    assert task.marks == 1         # и марка одна, а не 900


def test_workshift_retries_flaky_stand(cfg: Settings, monkeypatch):
    """500 «error proxy» — нестабильность стенда, а не отказ бизнес-логики.

    Шаги смены идемпотентны, поэтому повтор безопасен: тот же запрос проходит
    со второй попытки, и прогон не должен падать из-за этого.
    """
    from lo_api.workshift import WorkshiftClient

    monkeypatch.setattr(time, "sleep", lambda s: None)
    attempts: list[int] = []

    def flaky(self, url, json=None, headers=None, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            return _resp(500, {"error": {"code": 703, "message":
                                         "error proxy: Не удалось получить информацию о хабе"}})
        return _resp(200, {"result": {"token": "T" * 32}})

    monkeypatch.setattr(requests.Session, "post", flaky)
    cfg.picker_login, cfg.picker_password = "95371423", "x"

    assert WorkshiftClient(cfg).login("95371423", "x") == "T" * 32
    assert len(attempts) == 3, "должен был повторить два раза и получить токен"


def test_workshift_does_not_retry_real_errors(cfg: Settings, monkeypatch):
    """Отказ бизнес-логики повторять бессмысленно — падаем сразу."""
    from lo_api.workshift import WorkshiftClient, WorkshiftError

    attempts: list[int] = []

    def always_bad(self, url, json=None, headers=None, **kw):
        attempts.append(1)
        return _resp(200, {"error": {"code": 4283,
                                     "message": "Отсутствует обязательный параметр - username"}})

    monkeypatch.setattr(requests.Session, "post", always_bad)
    with pytest.raises(WorkshiftError, match="Отсутствует обязательный параметр"):
        WorkshiftClient(cfg).login("x", "y")
    assert len(attempts) == 1, "бизнес-ошибку повторять не нужно"


def test_workshift_requires_credentials(cfg: Settings):
    """Пустой логин ловится до сети.

    Стенд на пустой username отвечает «Отсутствует обязательный параметр -
    username», по которому не догадаться, что не заполнен .env.
    """
    from lo_api.workshift import WorkshiftClient, WorkshiftError

    cfg.picker_login = cfg.picker_password = ""
    with pytest.raises(WorkshiftError, match="LO_PICKER_LOGIN"):
        WorkshiftClient(cfg).start_picker()

    cfg.courier_login = cfg.courier_password = ""
    with pytest.raises(WorkshiftError, match="LO_COURIER_LOGIN"):
        WorkshiftClient(cfg).start_courier()


def test_tms_login_page_instead_of_form_is_explained():
    """Без входа TMS отдаёт страницу логина — ошибка должна называть причину."""
    from lo_api.tms import TmsError, _csrf

    with pytest.raises(TmsError, match="LO_TMS_USER"):
        _csrf("<html><body>Вы не авторизованы</body></html>")


def test_tms_requires_credentials(cfg: Settings):
    """Пустые логин и пароль ловятся до похода в сеть."""
    from lo_api.tms import TmsClient, TmsError

    cfg.tms_user = ""
    cfg.tms_password = ""
    with pytest.raises(TmsError, match="LO_TMS_PASSWORD"):
        TmsClient(cfg).login()


def _payment_list_response():
    return _resp(200, {"Body": {
        "PaymentTypeList": [
            {"Id": "702", "Name": "Списать с баланса"},
            {"Id": "2", "Name": "Оплата без привязки карты"},
            {"Id": "1", "Name": "При получении (картой/наличными)"},
        ],
        "DefaultPaymentType": {"typeId": 2},
    }})


def test_pickup_hides_card_payment(cfg: Settings, monkeypatch):
    """При самовывозе «Оплата без привязки карты» (2) неприменима.

    Стенд её всё равно отдаёт, причём описанием про доставку («после доставки
    спишем итоговую»), поэтому отсекаем на своей стороне.
    """
    from lo_api.flows import CheckoutFlow

    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _payment_list_response())
    api = LentaApi(Context(cfg=cfg, mission=Mission.PICKUP))
    types, default_id, chosen = CheckoutFlow(api).choose_payment(None)

    assert [str(p.id) for p in types] == ["702", "1"]
    # Способ по умолчанию тоже отсекается, иначе выбрали бы скрытый из списка
    assert default_id is None
    assert chosen == "702"


def test_delivery_keeps_card_payment(cfg: Settings, monkeypatch):
    """Для доставки тот же способ законен — фильтр привязан к миссии."""
    from lo_api.flows import CheckoutFlow

    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _payment_list_response())
    api = LentaApi(Context(cfg=cfg, mission=Mission.DELIVERY))
    types, default_id, chosen = CheckoutFlow(api).choose_payment(None)

    assert [str(p.id) for p in types] == ["702", "2", "1"]
    assert default_id == "2"
    assert chosen == "2"


def test_explicit_excluded_payment_is_rejected(cfg: Settings, monkeypatch):
    """Явно запрошенный неприменимый способ падает внятно, а не молча меняется."""
    from lo_api.flows import CheckoutFlow

    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _payment_list_response())
    api = LentaApi(Context(cfg=cfg, mission=Mission.PICKUP))
    with pytest.raises(LoApiError, match="неприменим к миссии"):
        CheckoutFlow(api).choose_payment("2")


def test_partner_card_comes_from_organization(cfg: Settings, monkeypatch):
    """Карта берётся у организации, а не из конфига.

    Прописанная в .env карта принадлежит одному юрлицу: под другим аккаунтом
    она молча уводила бы заказ в отказ, который стенд никак не объясняет.
    """
    sent: dict = {}

    def fake_request(self, method, url, **kw):
        if url.endswith("/b2b/legalEntity"):
            return _resp(200, {"legalEntityId": "ORG", "name": "ООО Тест",
                               "proCardNumber": "960000009999"})
        if "orderAdd" in url:
            sent["body"] = json.loads(kw["data"]["request"])["Body"]
            return _resp(200, {"Body": {"PendingRequestKey": "KEY", "OrderId": "1"}})
        return _resp(200, {"Head": {"Status": "success"}, "Body": {}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    cfg.partner_discount_card = "111111111111"  # чужая карта в конфиге
    api = LentaApi(Context(cfg=cfg))

    api.order.add("702", partner_card=api.b2b.card_number())
    assert sent["body"]["PartnerDiscountCard"] == "960000009999"


def test_order_add_without_card_explains_itself(cfg: Settings, monkeypatch):
    """Стенд отклоняет заказ без карты лояльности, но причину не называет.

    Приходит OrderCreationException с пустым Message и «повторите попытку» в
    Description. Подтверждено экспериментом: два одинаковых заказа, разница
    только в PartnerDiscountCard — без неё failure, с ней заказ создан.
    """
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Head": {"Status": "failure"}, "Body": {
            "ErrorList": [{"Code": 3548, "Message": "",
                           "Description": "Не удалось оформить заказ"}],
        }}),
    )
    cfg.partner_discount_card = ""
    api = LentaApi(Context(cfg=cfg))
    with pytest.raises(BusinessError, match="без карты лояльности"):
        api.order.add("702")


def test_http_error_raises(cfg: Settings, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(401, {"error": "unauthorized"}),
    )
    with pytest.raises(HttpStatusError) as exc:
        Transport(Context(cfg=cfg)).rest("cartLookup")
    assert exc.value.status == 401
    assert exc.value.trace_id, "в ошибке должен быть trace_id для поиска в трассировке"


def test_non_json_raises(cfg: Settings, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, text="<html>502 Bad Gateway</html>"),
    )
    with pytest.raises(NotJsonError):
        Transport(Context(cfg=cfg)).rest("cartLookup")


def test_order_add_is_not_retried(cfg: Settings, monkeypatch):
    """orderAdd неидемпотентен: повтор на 502 создал бы второй заказ."""
    calls = {"n": 0}

    def fake_request(self, method, url, **kw):
        calls["n"] += 1
        return _resp(502, {"error": "bad gateway"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    cfg.retry_attempts = 3
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    with pytest.raises(HttpStatusError):
        api.order.add("1")
    assert calls["n"] == 1, f"orderAdd не должен повторяться, было попыток: {calls['n']}"


def test_safe_call_is_retried(cfg: Settings, monkeypatch):
    calls = {"n": 0}

    def fake_request(self, method, url, **kw):
        calls["n"] += 1
        return _resp(503 if calls["n"] < 3 else 200, {"Body": {"Goods": []}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    cfg.retry_attempts = 3
    cfg.retry_backoff = 0
    Transport(Context(cfg=cfg)).rest("cartLookup")
    assert calls["n"] == 3


# --- доменная логика -------------------------------------------------------
def test_payment_find_handles_int_ids():
    """Регресс: сравнение int с str не находило способ по умолчанию."""
    types = [PaymentType(Id=1, Name="Безнал"), PaymentType(Id="2", Name="Карта")]
    assert PaymentClient.find(types, "1").name == "Безнал"
    assert PaymentClient.find(types, 2).name == "Карта"
    assert PaymentClient.find(types, "99") is None
    assert PaymentClient.find(types, None) is None


def test_interval_pick_skips_unavailable():
    """Регресс: старое intervals[2] брало третий из сырого списка."""
    intervals = [
        Interval(Id="a", Title="10-12", Disabled=True),
        Interval(Id="b", Title="12-14", Disabled=False),
        Interval(Id="c", Title="14-16", Disabled=False),
    ]
    api = LentaApi(Context(cfg=Settings(_env_file=None)))
    assert api.delivery.pick_interval(intervals, 0).id == "b"
    assert api.delivery.pick_interval(intervals, 1).id == "c"
    assert api.delivery.pick_interval(intervals, 99).id == "c", "индекс за пределами → последний"


def test_address_kind_from_flags():
    assert Address(Id=1, isAddressMercury=False, isAddressEgais=False).kind is AddressKind.REGULAR
    assert Address(Id=2, isAddressMercury=True, isAddressEgais=False).kind is AddressKind.MERCURY
    assert Address(Id=3, isAddressMercury=False, isAddressEgais=True).kind is AddressKind.EGAIS


def test_cart_quantity_map():
    """SKU позиции приходит как GoodsItemId — подтверждено HAR.

    Раньше модель ждала Id, позиции не разбирались никогда, и корзина всегда
    выглядела пустой по составу при непустой сумме.
    """
    cart = Cart(Goods=[{"GoodsItemId": "433217", "Quantity": 2},
                       {"GoodsItemId": "555", "Quantity": 1.5}])
    assert cart.quantity_by_sku == {"433217": 2, "555": 1.5}
    assert not cart.is_empty
    assert Cart().is_empty


def test_cart_goods_parsed_from_cart_list():
    """Регресс: позиции ищутся в CartList[0].Goods, а не по выдуманным ключам."""
    cart = Cart.from_body({"CartList": [{
        "TotalCost": "3669.49", "PositionsCount": 1,
        "Goods": [{"GoodsItemId": "520787", "Name": "Кофе", "Quantity": 1,
                   "Price": 3669.49}],
    }]})
    assert cart.quantity_by_sku == {"520787": 1}
    assert cart.total == 3669.49
    assert cart.goods[0].title == "Кофе"


def test_order_add_sends_vsd_as_none_not_string(cfg: Settings, monkeypatch):
    """Регресс: было "AddressVSD": "null" — строка вместо значения."""
    captured: dict = {}

    def fake_request(self, method, url, **kw):
        captured["body"] = json.loads(kw["data"]["request"])
        return _resp(200, {"Body": {"PendingRequestKey": "KEY"}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg, session_token="T"))

    api.order.add("1")
    assert captured["body"]["Body"]["ProData"]["AddressVSD"] is None

    api.order.add("1", address_vsd="VSD-123")
    assert captured["body"]["Body"]["ProData"]["AddressVSD"] == "VSD-123"


def test_order_add_requires_pending_key(cfg: Settings, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Body": {}}),
    )
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    with pytest.raises(BusinessError, match="PendingRequestKey"):
        api.order.add("1")


def test_wait_for_order_polls_until_ready(cfg: Settings, monkeypatch):
    """Регресс: responseLookup звался один раз, немедленно — гонка."""
    calls = {"n": 0}

    def fake_request(self, method, url, **kw):
        calls["n"] += 1
        orders = [{"Id": "ORDER-1"}] if calls["n"] >= 3 else []
        return _resp(200, {"Body": {"Response": {"Body": {"OrderList": orders}}}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    cfg.order_poll_interval = 0
    api = LentaApi(Context(cfg=cfg, session_token="T"))

    order = api.order.wait_for_order("KEY")

    assert str(order.id) == "ORDER-1"
    assert calls["n"] == 3


def test_wait_for_order_times_out(cfg: Settings, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Body": {"Response": {"Body": {"OrderList": []}}}}),
    )
    cfg.order_poll_interval = 0
    cfg.order_wait_timeout = 0.05
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    with pytest.raises(Exception, match="не появился"):
        api.order.wait_for_order("KEY")


# --- авторизация: лимиты стенда и путь verify ------------------------------
def test_request_code_tolerates_already_sent(cfg: Settings, monkeypatch):
    """Регресс: «Вы уже запрашивали код» (HTTP 500) валило все тесты после первого."""
    body = {"code": "ERROR_INTERNAL", "message": "Вы уже запрашивали код для входа"}
    for escaped in (False, True):
        monkeypatch.setattr(
            requests.Session, "request",
            lambda self, m, u, _e=escaped, **kw: _resp(500, body, ensure_ascii=_e),
        )
        api = LentaApi(Context(cfg=cfg))

        assert api.auth.request_code("79990001122") is False, (
            f"повторный запрос кода — не ошибка теста (ensure_ascii={escaped})"
        )


def test_request_code_still_raises_on_real_error(cfg: Settings, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(
            500, {"code": "ERROR_INTERNAL", "message": "что-то сломалось"}
        ),
    )
    api = LentaApi(Context(cfg=cfg))
    with pytest.raises(HttpStatusError):
        api.auth.request_code("79990001122")


def test_verify_falls_back_on_404(cfg: Settings, monkeypatch):
    """404 на /pro/... → одна попытка по запасному пути, с WARNING в логе."""
    seen: list[str] = []

    def fake_request(self, method, url, **kw):
        seen.append(url)
        from urllib.parse import urlparse
        if urlparse(url).path == cfg.auth_verify_path:
            return _resp(404, text="Page not found")
        return _resp(200, {"token": "T" * 16, "passportId": "PID"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg))

    payload = api.auth.verify_code("9430000014", "1234")

    assert payload["token"] == "T" * 16
    assert len(seen) == 2, f"ожидались основной и запасной путь, было: {seen}"
    assert seen[1].endswith(cfg.auth_verify_fallback_path)


def test_verify_fallback_can_be_disabled(cfg: Settings, monkeypatch):
    cfg.auth_verify_autofallback = False
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(404, text="Page not found"),
    )
    api = LentaApi(Context(cfg=cfg))
    with pytest.raises(HttpStatusError) as exc:
        api.auth.verify_code("9430000014", "1234")
    assert exc.value.status == 404


def test_auth_paths_come_from_config(cfg: Settings, monkeypatch):
    cfg.auth_send_path = "/custom/send"
    seen: list[str] = []

    def fake_request(self, method, url, **kw):
        seen.append(url)
        return _resp(200, {})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    LentaApi(Context(cfg=cfg)).auth.send_code("9430000014")
    assert seen[0].endswith("/custom/send")


def test_flow_authorize_is_idempotent(cfg: Settings, monkeypatch):
    """Регресс: повторный login в каждом тесте упирался в лимит стенда."""
    from lo_api.flows import CheckoutFlow

    calls: list[str] = []

    def fake_request(self, method, url, **kw):
        calls.append(url)
        if "token/guest" in url:
            return _resp(200, {"sessionId": "GUEST"})
        if "legalEntity/person" in url:
            return _resp(200, {"list": [{"legalEntityId": "ORG", "name": "ООО Тест",
                                         "inn": "1234567890", "kpp": "1", "status": "active"}]})
        return _resp(200, {"token": "T" * 16, "passportId": "PID"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg))
    flow = CheckoutFlow(api)

    flow.authorize("79990001122", "1234")
    logins_after_first = sum(1 for u in calls if "auth/code" in u)
    flow.authorize("79990001122", "1234")
    logins_after_second = sum(1 for u in calls if "auth/code" in u)

    assert logins_after_second == logins_after_first, (
        "повторная авторизация не должна снова запрашивать код"
    )


# --- сессия: готовый токен и кэш ------------------------------------------
def test_login_requires_explicit_code(cfg: Settings):
    """Регресс: раньше по умолчанию слался «1234», стенд отвечал 400
    BAD_REQUEST, и это выглядело как поломка клиента, а не неверный код."""
    cfg.sms_stub_code = ""
    api = LentaApi(Context(cfg=cfg))
    with pytest.raises(LoApiError, match="не задан код входа"):
        api.auth.login("79990001122")


def test_wrong_code_error_is_actionable(cfg: Settings, monkeypatch):
    """400 на verify должен объяснять, что делать, а не просто отдавать тело."""
    def fake_request(self, method, url, **kw):
        if "token/guest" in url:
            return _resp(200, {"sessionId": "GUEST"})
        if "send" in url:
            return _resp(200, {})
        return _resp(400, {"code": "BAD_REQUEST", "message": "Bad request"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg))
    with pytest.raises(HttpStatusError, match="Возможные причины"):
        api.auth.login("79990001122", "1234")


def test_use_token_skips_auth(cfg: Settings, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: (calls.append(u), _resp(200, {"Body": {}}))[1],
    )
    api = LentaApi(Context(cfg=cfg))

    api.auth.use_token("TOKEN", "PID")

    assert api.ctx.session_token == "TOKEN"
    assert api.ctx.passport_id == "PID"
    assert not calls, "готовый токен не должен вызывать сеть"
    assert api.t._dynamic_headers()["sessiontoken"] == "TOKEN"


def test_session_cache_roundtrip(tmp_path):
    from lo_api.session_cache import CachedSession, clear, load, save

    path = str(tmp_path / "session.json")
    assert load(path) is None, "пустого кэша быть не должно"

    save(path, CachedSession(phone="79990001122", token="T", passport_id="P",
                             legal_entity_id="ORG", device_id="D"))
    restored = load(path, phone="79990001122")

    assert restored is not None
    assert (restored.token, restored.passport_id, restored.legal_entity_id) == ("T", "P", "ORG")

    assert load(path, phone="9990000000") is None, "чужая сессия не должна подходить"
    assert load(path, max_age=-1) is None, "протухшая сессия не должна подходить"

    clear(path)
    assert load(path) is None


def test_session_cache_survives_broken_file(tmp_path):
    from lo_api.session_cache import load

    path = tmp_path / "session.json"
    path.write_text("{не json", encoding="utf-8")
    assert load(str(path)) is None, "битый кэш игнорируется, а не роняет запуск"


def test_is_session_alive(cfg: Settings, monkeypatch):
    api = LentaApi(Context(cfg=cfg))
    assert api.auth.is_session_alive() is False, "без токена сессия не живая"

    api.auth.use_token("TOKEN")
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _resp(200, {"phone": "79990001122"}))
    assert api.auth.is_session_alive() is True

    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, **kw: _resp(401, {"error": "unauthorized"}))
    assert api.auth.is_session_alive() is False


# --- разбор аргументов lo-order -------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["433217"], [("433217", 1.0)]),
        (["433217:2"], [("433217", 2.0)]),
        (["433217:2, 555:3"], [("433217", 2.0), ("555", 3.0)]),
        (["433217:1.5"], [("433217", 1.5)]),
        (["111", "222:4"], [("111", 1.0), ("222", 4.0)]),
    ],
)
def test_parse_sku(raw: list[str], expected: list[tuple[str, float]]):
    from lo_api.cli.order import parse_sku

    assert parse_sku(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["abc", "433217:0", "433217:-1", "433217:xx", "433217:1,5"],
    ids=["буквы", "ноль", "отрицательное", "не-число", "запятая-в-дробном"],
)
def test_parse_sku_rejects_garbage(raw: str):
    from lo_api.cli.order import parse_sku

    with pytest.raises(SystemExit):
        parse_sku([raw])


def test_order_cli_parses_flags():
    from lo_api.cli.order import build_parser

    args = build_parser().parse_args(
        ["--mission", "pickup", "--sku", "433217:2", "--sku", "555", "--yes", "--dry-run"]
    )
    assert args.mission == "pickup"
    assert args.sku == ["433217:2", "555"]
    assert args.yes and args.dry_run


# --- контракт, подтверждённый HAR-записью реального оформления ------------
def test_auth_bodies_match_browser(cfg: Settings, monkeypatch):
    """Браузер шлёт ровно {phone, acceptMarketingCommunications} и {phone, code}.

    Лишние kfpVn/kfpKsid раньше уходили в запрос — в HAR их нет.
    """
    bodies: list[dict] = []

    def fake_request(self, method, url, **kw):
        if "token/guest" in url:
            return _resp(200, {"sessionId": "GUEST", "expiresIn": 3600})
        bodies.append(json.loads(kw["data"]))
        return _resp(200, {"token": "T" * 16, "passportId": "PID", "isB2B": True})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    LentaApi(Context(cfg=cfg)).auth.login("79990001122", "1234")

    assert bodies[0] == {"phone": "79990001122", "acceptMarketingCommunications": False}
    assert bodies[1] == {"phone": "79990001122", "code": "1234"}


def test_verify_result_accepts_real_schema():
    """Реальный ответ verify: {isB2B, isNewUser, jwt:{...}, passportId, token}."""
    from lo_api.models import VerifyResult

    result = VerifyResult.model_validate({
        "isB2B": True, "isNewUser": False,
        "jwt": {"access": "eyJ...", "refresh": "eyJ..."},
        "passportId": "a6e928f0-2f33-4fbd-88b1-c7daa24d3435",
        "token": "159DD19022CF1DBEC406B7CDE1D7F2CD",
    })
    assert result.token == "159DD19022CF1DBEC406B7CDE1D7F2CD"
    assert result.passport_id == "a6e928f0-2f33-4fbd-88b1-c7daa24d3435"
    assert result.is_b2b is True


def test_cart_parsed_from_cart_list():
    """Корзина лежит в Body.CartList[0], а не в Body напрямую."""
    cart = Cart.from_body({"CartList": [{
        "TotalCost": 3669.49, "PositionsCount": 1,
        "DeliveryIntervalId": "A_15046759_PLP_43_1788519600_1788521400",
        "DeliveryStoreId": 3429, "UserAddressId": 15046759, "DeliveryType": "delivery",
    }]})
    assert cart.total == 3669.49
    assert cart.interval_id == "A_15046759_PLP_43_1788519600_1788521400"
    assert cart.store_id == 3429
    assert not cart.is_empty


def test_empty_cart_detected_by_positions_count():
    assert Cart.from_body({"CartList": [{"TotalCost": "0.00", "PositionsCount": 0}]}).is_empty
    assert Cart.from_body({}).is_empty


def test_interval_availability_from_disabled_flag():
    """Стенд отдаёт Disabled, а не IsAvailable."""
    raw = {"Id": "A_15046759_PLP_43_1788519600_1788521400", "Date": "2026-09-04",
           "Disabled": False, "TimeFrom": "14:00:00", "TimeTo": "14:30:00",
           "Price": 99, "Title": "04.09.2026 пятница с 14:00 до 14:30"}
    assert Interval.model_validate(raw).is_available is True
    assert Interval.model_validate({**raw, "Disabled": True}).is_available is False


def test_intervals_request_carries_payment_type(cfg: Settings, monkeypatch):
    """PaymentTypeId уходит в запрос интервалов числом — как в браузере."""
    captured: dict = {}

    def fake_request(self, method, url, **kw):
        captured["body"] = json.loads(kw["data"]["request"])["Body"]
        return _resp(200, {"Body": {"DeliveryPlannedProIntervalList": []}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    ctx = Context(cfg=cfg, session_token="T", mission=Mission.DELIVERY,
                  selected_address_id=15046759)
    LentaApi(ctx).delivery.intervals(payment_type_id="702")

    assert captured["body"]["PaymentTypeId"] == 702, "должно уйти числом, а не строкой"
    assert captured["body"]["UserAddressId"] == 15046759


def test_order_add_sends_numeric_payment_type(cfg: Settings, monkeypatch):
    captured: dict = {}

    def fake_request(self, method, url, **kw):
        captured["body"] = json.loads(kw["data"]["request"])["Body"]
        return _resp(200, {"Head": {"Status": "pending"},
                           "Body": {"PendingRequestKey": "KEY", "OrderId": "900001126105"}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg, session_token="T"))

    key = api.order.add("702")

    assert key == "KEY"
    assert captured["body"]["PaymentTypeId"] == 702
    assert captured["body"]["ProData"]["AddressVSD"] is None
    assert api.ctx.extra["last_order_id"] == "900001126105", "OrderId приходит сразу"


def test_cart_modify_has_no_noproductaction(cfg: Settings, monkeypatch):
    """Браузер шлёт только DeliveryIntervalId; NoProductActionID — отдельный эндпоинт."""
    captured: dict = {}

    def fake_request(self, method, url, **kw):
        captured["body"] = json.loads(kw["data"]["request"])["Body"]
        return _resp(200, {"Body": {"CartList": [{}]}})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    api = LentaApi(Context(cfg=cfg, session_token="T"))
    api.cart.set_interval(Interval(Id="A_1_PLP_43_1_2"))

    assert captured["body"] == {"DeliveryIntervalId": "A_1_PLP_43_1_2"}


def test_head_status_failure_raises(cfg: Settings, monkeypatch):
    """Бизнес-статус легаси-конверта живёт в Head.Status."""
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Head": {"Status": "error"}, "Body": {}}),
    )
    with pytest.raises(BusinessError, match="Status"):
        Transport(Context(cfg=cfg)).rest("cartLookup")


@pytest.mark.parametrize("status", ["success", "pending"])
def test_head_status_ok_passes(cfg: Settings, monkeypatch, status: str):
    """pending — нормальный ответ orderAdd, ошибкой считать нельзя."""
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, m, u, **kw: _resp(200, {"Head": {"Status": status}, "Body": {}}),
    )
    Transport(Context(cfg=cfg)).rest("orderAdd")


# --- марки ОСУ уходят отдельным методом -----------------------------------
def _marking_order(kind_flags: dict) -> dict:
    """Заказ из одной позиции с заданными признаками маркировки."""
    return {
        "hubOrderId": "900001", "statusInfo": {"alias": "ACCEPTED"},
        "itemsList": [{
            "sku": "189454", "name": "Вода", "barcode": "4770170000068",
            "count": 1, "countPick": 0, "unit": "PC",
            "stocks": [{"id": 7, "storeId": 3429, "count": 1}],
            "batches": [], **kind_flags,
        }],
    }


@pytest.mark.parametrize(
    ("flags", "expected", "forbidden"),
    [
        # ОСУ: isOsuMarkRequired. Подтверждено записью сборки — sku 189454
        ({"isOsuMarkRequired": True, "isMarkingInGoods": True, "controlType": None},
         "sapMarking", "itemIncreaseByMarking"),
        # ПЭУ партионный: isBatchMarking. В записи — sku 575098
        ({"isBatchMarking": True, "isMarking": True, "isMarkingInGoods": True,
          "controlType": "V"},
         "itemBatchIncreaseByMarking", "sapMarking"),
    ],
    ids=["осу-sapMarking", "пэу-партионный-itemBatchIncreaseByMarking"],
)
def test_marking_method_matches_item_kind(cfg: Settings, monkeypatch,
                                          flags: dict, expected: str, forbidden: str):
    """Метод отправки марки выбирается по типу позиции, а не один на всех.

    Раньше ОСУ уходил общим itemIncreaseByMarking и марка не доезжала.
    """
    from lo_api.picking import PickingClient, PickTask, _do_marks, build_plan

    calls: list[str] = []

    def fake_call(self, method, params=None, **kw):
        calls.append(method)
        return {}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    monkeypatch.setattr(PickingClient, "goods_card", lambda self, task: {})

    plan = build_plan(_marking_order(flags))
    task: PickTask = plan.tasks[0]
    assert task.marks == 1, f"ожидалась одна марка, тип {task.kind}"

    client = PickingClient(cfg)
    client.token = "T"
    _do_marks(client, "900001", task, batch=task.kind == "MARK_PEU_BATCH")

    assert expected in calls, f"тип {task.kind}: ожидали {expected}, было {calls}"
    assert forbidden not in calls, f"тип {task.kind}: {forbidden} отправляться не должен"


def test_osu_mark_carries_sku_and_order(cfg: Settings, monkeypatch):
    """sapMarking принимает sku и orderId — в отличие от itemBatchIncreaseByMarking."""
    from lo_api.picking import PickingClient

    sent: dict = {}

    def fake_call(self, method, params=None, **kw):
        sent[method] = params
        return {}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    client = PickingClient(cfg)
    client.sap_marking("900001126377", "189454", "0104770170000068215UPe!B")

    assert sent["sapMarking"] == {
        "sku": "189454", "orderId": "900001126377",
        "marking": "0104770170000068215UPe!B",
    }


@pytest.mark.parametrize("units, expected_count", [(1, False), (3, True)])
def test_osu_unit_added_by_rescan_after_mark(cfg: Settings, monkeypatch,
                                             units: int, expected_count: bool):
    """ОСУ: единицу кладёт повторный скан ШК после марки, а не сама марка.

    Запись Charles по заказу 900001126466: itemUpdateByBarcode → countPick null,
    sapMarking → null, повторный itemUpdateByBarcode → 1. Количество больше
    одной единицы доставляется уже после повторного скана. В sapMarking sku
    уходит как в заказе, с нулями: на «8297» стенд ответил 3347 «SKU товара
    не совпадает с SKU полученным по GTIN», приложение шлёт «008297».
    """
    from lo_api.picking import PickingClient, _pick_task, build_plan

    sent: list[tuple[str, dict]] = []

    def fake_call(self, method, params=None, **kw):
        sent.append((method, params))
        return {}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    monkeypatch.setattr(PickingClient, "goods_card",
                        lambda self, t: {"markType": "CHEMISTRY", "isAlcohol": False})
    order = {"warehouses": [{"id": 7777, "storeId": 12}], "itemsList": [{
        "sku": "008297", "name": "Порошок FINISH", "barcode": "4607109402474",
        "count": units, "isMarkingInGoods": True, "isOsuMarkRequired": True,
        "isBatchMarking": False, "controlType": None,
        "stockData": {"records": [{"count": units, "countPick": None,
                                   "id": 7777, "storeId": 12}]}}]}
    task = build_plan(order).tasks[0]
    _pick_task(PickingClient(cfg), "900001126466", task)

    methods = [m for m, _ in sent]
    assert methods[:3] == ["itemUpdateByBarcode", "sapMarking", "itemUpdateByBarcode"]
    assert ("orderUpdate" in methods) is expected_count
    assert sent[1][1]["sku"] == "008297", "sapMarking — sku с нулями, как в заказе"
    assert sent[0][1]["barcode"] == "4607109402474"


def _ask_order(*items: dict) -> dict:
    return {"warehouses": [{"id": 1, "storeId": 12}], "itemsList": list(items)}


def _ask_item(sku: str, name: str, count: int, **flags) -> dict:
    return {"sku": sku, "name": name, "barcode": "4600000000017", "count": count,
            "missingReasons": {"3": "Не найден в ТЗ ", "1": "Срок годности"},
            "stockData": {"records": [{"count": count, "countPick": None,
                                       "id": 1, "storeId": 12}]}, **flags}


def _ask_client(cfg: Settings, monkeypatch, answers: list[str]):
    from lo_api.cli.order import AskDecisions
    from lo_api.picking import PickingClient

    sent: list[tuple[str, dict]] = []

    def fake_call(self, method, params=None, **kw):
        sent.append((method, params))
        if method == "sapManufacturer":
            return {"manufacturers": [{"code": "A1", "name": "ПП для Ленты", "inn": "7800"},
                                      {"code": "B2", "name": "Другой"}]}
        return {}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    monkeypatch.setattr(PickingClient, "goods_card", lambda self, t: {"isAlcohol": False})
    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: answers.pop(0))
    client = PickingClient(cfg)
    client.decisions = AskDecisions()
    return client, sent


def test_ask_mode_removes_item_with_reason(cfg: Settings, monkeypatch):
    """«Убрать из заказа» — orderUpdate с missingReasonId, как в приложении.

    Причины приходят прямо в позиции (missingReasons). Убранная позиция больше
    не собирается и не считается недосбором: в заказе 900001126255 у неё
    missingReasonId 3 и countPick 0.
    """
    from lo_api.picking import _assemble_items, build_plan, picked_issues

    order = _ask_order(_ask_item("520787", "Кофе", 2), _ask_item("433217", "Чай", 1))
    # кофе: «Убрать», причина 2-я в списке («Срок годности»); чай: «Собрать»
    client, sent = _ask_client(cfg, monkeypatch, ["2", "2", "1"])
    monkeypatch.setattr("lo_api.picking._validate_sap", lambda c, o: None)
    _assemble_items(client, order, "900001", allow_partial=False, resume=False)

    removal = next(p for m, p in sent if m == "orderUpdate" and "items" in p.get("order", {}))
    assert removal["order"]["items"] == [
        {"sku": "520787", "missingReasonId": "1", "missingReasonComment": ""}]
    scanned = [p["barcode"] for m, p in sent if m == "itemUpdateByBarcode"]
    assert len(scanned) == 1, "убранный кофе не сканируется, чай — да"

    removed = {**_ask_item("520787", "Кофе", 2), "countPick": 0, "missingReasonId": 1}
    assert picked_issues({"itemsList": [removed]}) == [], "уборка — не недосбор"
    plan = build_plan(_ask_order(removed), resume=True)
    assert plan.tasks == [] and "убран из заказа" in plan.picked[0]


def test_ask_mode_several_batches_in_one_sapbatch(cfg: Settings, monkeypatch):
    """Партионный: несколько партий уходят одним sapBatch, количества в сумме — весь товар."""
    from lo_api.picking import _pick_task, build_plan

    task = build_plan(_ask_order(_ask_item("64536", "Корм", 5, controlType="V"))).tasks[0]
    # 2 партии; 1-я: производитель 2-й, дата, 3 шт; 2-я: Enter (тот же), дата → остаток 2
    client, sent = _ask_client(cfg, monkeypatch,
                               ["2", "2", "2025-12-26", "3", "", "01.02.2026"])
    _pick_task(client, "900001", task)

    batch = next(p for m, p in sent if m == "sapBatch")
    assert [(b["manufacturerCode"], b["dateOfManufacture"], b["count"], b["batchPosition"])
            for b in batch["batches"]] == [("B2", "2025-12-26", 3, 0),
                                           ("B2", "2026-02-01", 2, 1)]


def test_ask_mode_peu_batches_go_batch_then_its_marks(cfg: Settings, monkeypatch):
    """ПЭУ с партиями: партия → её марки → следующая партия → её марки.

    Все партии сразу заводить нельзя — SAP отвечает «только одна несобранная
    партия может быть без ИНН». Дата в sapBatchAdd — ДД.ММ.ГГГГ.
    """
    from lo_api.picking import _pick_task, build_plan

    item = _ask_item("575098", "Икра", 3, isMarkingInGoods=True, controlType="V")
    task = build_plan(_ask_order(item)).tasks[0]
    client, sent = _ask_client(cfg, monkeypatch, ["2", "", "2026-02-26", "2", "", "2026-03-01"])
    _pick_task(client, "900001", task)

    steps = [m for m, _ in sent if m in ("sapBatchAdd", "itemBatchIncreaseByMarking")]
    assert steps == ["sapBatchAdd", "itemBatchIncreaseByMarking", "itemBatchIncreaseByMarking",
                     "sapBatchAdd", "itemBatchIncreaseByMarking"]
    adds = [p for m, p in sent if m == "sapBatchAdd"]
    assert [a["dateOfManufacture"] for a in adds] == ["26.02.2026", "01.03.2026"]
    assert adds[0]["manufacturerInn"] == "7800", "производитель по умолчанию — из настроек"


def test_tms_form_notes_are_read():
    """Ответ формы TMS разбирается: сообщения Django и ошибки полей.

    Без этого «назначено» не отличить от «не принято» — оба ответа HTTP 200.
    """
    from lo_api.tms import form_notes

    page = ('<ul class="messagelist"><li class="success">Заказ перенесён</li>'
            '<li class="warning">Курьер без&nbsp;ТС</li></ul>'
            '<ul class="errorlist"><li>Выберите корректный вариант.</li></ul>')
    assert form_notes(page) == ["успех: Заказ перенесён", "предупреждение: Курьер без ТС",
                                "ошибка: Выберите корректный вариант."]
    assert form_notes("<html>форма без сообщений</html>") == []


def _assign_setup(cfg: Settings, monkeypatch, applied_after: int | None):
    """TMS всегда отвечает «ок», а назначение доходит до заказа только с N-й отправки."""
    from lo_api.cli import order as cli
    from lo_api.picking import PickingClient
    from lo_api.tms import OrderPage, Performer, TmsClient

    state = {"posts": 0}
    page = OrderPage(csrf="c", action="Сохранить",
                     pickers=[Performer("95372156", "Пикерок БиТуБи (95372156)")],
                     couriers=[Performer("95372157", "Курок БиТуБи (95372157)")])
    monkeypatch.setattr(TmsClient, "login", lambda self: None)
    monkeypatch.setattr(TmsClient, "open_order", lambda self, order_id: page)

    def assign(self, order_id, page, *, picker, courier=None):
        state["posts"] += 1
        return []

    monkeypatch.setattr(TmsClient, "assign", assign)

    def order_get(self, order_id):
        done = applied_after is not None and state["posts"] >= applied_after
        real = {"buyerId": 95372156, "name": "Пикерок БиТуБи"}
        return {"pickerStaffInfo": real if done else {"buyerId": 2147483647, "name": None},
                "courierStaffInfo": {"buyerId": 95372157, "name": "Курок"} if done
                else {"buyerId": 436130383858, "name": "Dummy"}}

    monkeypatch.setattr(PickingClient, "order_get", order_get)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cfg.picker_login, cfg.courier_login = "95372156", "95372157"
    client = PickingClient(cfg)
    args = cli.build_parser().parse_args(["-y"])
    return cli, client, args, state


def test_tms_assignment_is_checked_and_resent(cfg: Settings, monkeypatch):
    """Назначение проверяется по заказу, а не по HTTP 200 от TMS.

    Заказ 900001126510: форма «принята», а в orderGet сборщик пустой, курьер
    Dummy — и сборка упала на sapValidate. Теперь форма отправляется ещё раз.
    """
    cli, client, args, state = _assign_setup(cfg, monkeypatch, applied_after=2)
    cli.assemble_order(cfg, "900001126510", args, client)
    assert state["posts"] == 2, "со второй отправки назначение дошло"


def test_tms_assignment_never_applied_stops_before_picking(cfg: Settings, monkeypatch):
    """Не назначился за все попытки — стоп с объяснением, сборка не начинается."""
    cli, client, args, state = _assign_setup(cfg, monkeypatch, applied_after=None)
    with pytest.raises(SystemExit, match="так и не назначен: не хватает — сборщик, курьер"):
        cli.assemble_order(cfg, "900001126510", args, client)
    assert state["posts"] == cfg.tms_assign_attempts


def test_delivery_does_not_track_assembly_flags(cfg: Settings, monkeypatch, caplog):
    """Курьер не следит за собственными нуждами и ВСД — они нужны только сборке."""
    import logging

    from lo_api.picking import PickingClient, deliver_order

    pro = {"markOwnNeeds": True, "vetOwnNeeds": True, "vetCertificate": "Не нужны"}
    status = {"alias": "ASSEMBLE_COMPLETE"}

    def fake_call(self, method, params=None, **kw):
        if method == "orderUpdate":
            status["alias"] = params["order"]["status"]
        order = {"id": "900001126510", "statusInfo": dict(status), "proInfo": pro}
        return {"order": order} if method in ("orderGet", "orderUpdate") else {}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    courier = PickingClient(cfg)
    with caplog.at_level(logging.INFO, logger="lo_api.picking"):
        deliver_order(courier, "900001126510")

    text = caplog.text
    assert "DELIVERED" in text
    assert "ВСД" not in text and "собственные нужды" not in text
    assert courier.vsd_required is None and courier.own_needs is False


def test_assembly_flags_tracked_only_while_picking(cfg: Settings, monkeypatch, caplog):
    """СН и ВСД читаются только на время сборки — не при проверке назначения.

    Вне pick_order (проверка назначения, выбор миссии) `ВСД по заказу …` в
    журнал не попадает. Признаки сбрасываются на каждую сборку: клиент может
    собрать подряд чужой открытый заказ и свой.
    """
    import logging

    from lo_api.picking import PickingClient

    def fake_call(self, method, params=None, **kw):
        return {"order": {"statusInfo": {"alias": "DELIVERED"},
                          "proInfo": {"vetCertificate": "Нужны"}}}

    monkeypatch.setattr(PickingClient, "call", fake_call)
    client = PickingClient(cfg)
    with caplog.at_level(logging.INFO, logger="lo_api.picking"):
        client.order_get("900001126511")          # как при проверке назначения
    assert "ВСД" not in caplog.text and client.vsd_required is None

    from lo_api.picking import pick_order
    client.vsd_required, client.own_needs = False, True   # «хвост» прошлого заказа
    with caplog.at_level(logging.INFO, logger="lo_api.picking"):
        pick_order(client, "900001126511")
    assert client.vsd_required is True, "новая сборка читает признаки заново"
    assert client.own_needs is False and client.tracks_assembly_flags is False


# --- возврат к прошлому шагу ----------------------------------------------
def test_back_returns_to_previous_step():
    """«Назад» переигрывает предыдущий шаг, а не начинает всё заново."""
    from lo_api.cli.steps import Back, Step, run_steps

    log: list[str] = []
    attempts = {"оплата": 0}

    def payment() -> None:
        log.append("оплата")
        attempts["оплата"] += 1

    def interval() -> None:
        log.append("интервал")
        # Первый заход — человек понял, что выбрал не ту оплату
        if attempts["оплата"] == 1:
            raise Back

    run_steps([Step("", lambda: log.append("адрес")),
               Step("", payment),
               Step("", interval)], say=lambda _: None)

    assert log == ["адрес", "оплата", "интервал", "оплата", "интервал"]


def test_back_skips_irreversible_steps():
    """Возврат проходит мимо шагов, в которые возвращаться нельзя."""
    from lo_api.cli.steps import Back, Step, previous_reversible, run_steps

    steps = [Step("", lambda: None),
             Step("", lambda: None, reversible=False),
             Step("", lambda: None)]
    assert previous_reversible(steps, 2) == 0, "необратимый шаг не может быть целью"

    log: list[str] = []
    done = {"n": 0}

    def last() -> None:
        log.append("последний")
        done["n"] += 1
        if done["n"] == 1:
            raise Back

    run_steps([Step("", lambda: log.append("выбор")),
               Step("", lambda: log.append("создание"), reversible=False),
               Step("", last)], say=lambda _: None)

    assert log == ["выбор", "создание", "последний", "выбор", "создание", "последний"]


def test_back_on_first_step_repeats_it():
    """С первого шага возвращаться некуда — он просто спрашивает заново."""
    from lo_api.cli.steps import Back, Step, run_steps

    tries = {"n": 0}

    def first() -> None:
        tries["n"] += 1
        if tries["n"] == 1:
            raise Back

    said: list[str] = []
    run_steps([Step("", first)], say=said.append)

    assert tries["n"] == 2
    assert any("возвращаться некуда" in s for s in said)


def test_stop_ends_sequence_without_remaining_steps():
    """--dry-run завершает сценарий, не создавая заказ."""
    from lo_api.cli.steps import Step, Stop, run_steps

    log: list[str] = []

    def confirm() -> None:
        raise Stop

    run_steps([Step("", lambda: log.append("корзина")),
               Step("", confirm),
               Step("", lambda: log.append("создание заказа"))], say=lambda _: None)

    assert log == ["корзина"], "шаги после Stop выполняться не должны"


# --- ошибка шага --------------------------------------------------------
def test_error_can_send_user_back():
    """Отказ стенда предлагает вернуться и выбрать иначе, а не роняет прогон."""
    from lo_api.cli.steps import Step, run_steps

    log: list[str] = []
    fails = {"n": 0}

    def interval() -> None:
        log.append("интервал")
        fails["n"] += 1
        if fails["n"] == 1:
            raise RuntimeError("интервал уже занят")

    run_steps([Step("", lambda: log.append("оплата")), Step("", interval)],
              on_error=lambda exc, step: "back", say=lambda _: None)

    assert log == ["оплата", "интервал", "оплата", "интервал"], (
        "после отказа стенда шаг оплаты проходит заново, затем снова интервал"
    )


def test_error_can_retry_same_step():
    from lo_api.cli.steps import Step, run_steps

    tries = {"n": 0}

    def flaky() -> None:
        tries["n"] += 1
        if tries["n"] < 3:
            raise RuntimeError("стенд моргнул")

    run_steps([Step("", flaky)], on_error=lambda exc, step: "retry", say=lambda _: None)
    assert tries["n"] == 3


def test_error_without_handler_propagates():
    """Без диалога (CI) ошибка пробрасывается — молча переигрывать нельзя."""
    from lo_api.cli.steps import Step, run_steps

    with pytest.raises(RuntimeError, match="стенд лёг"):
        run_steps([Step("", lambda: (_ for _ in ()).throw(RuntimeError("стенд лёг")))],
                  say=lambda _: None)


def test_unreachable_error_aborts_instead_of_looping(monkeypatch):
    """Недоступный сервис прерывает сценарий: переигрывать шаг бессмысленно."""
    import argparse

    from lo_api.cli.order import _step_error_handler
    from lo_api.cli.steps import Step

    monkeypatch.setattr("lo_api.cli.order._interactive", lambda: True)
    exc = RuntimeError("сервис или его база недоступны")
    exc.unreachable = True

    decision = _step_error_handler(argparse.Namespace(yes=False))(exc, Step("", lambda: None))
    assert decision == "abort"


# --- пункт «назад» в самом вводе ------------------------------------------
@pytest.mark.parametrize("answer", ["3", "<", "назад", "Назад", "b"],
                         ids=["номер-пункта", "символ", "слово", "с-заглавной", "латиница"])
def test_choose_offers_back_as_a_menu_item(monkeypatch, capsys, answer: str):
    """Возврат виден в меню и срабатывает, как бы его ни выбрали."""
    from lo_api.cli import order as cli
    from lo_api.cli.steps import BACK_LABEL, Back

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: answer)

    with pytest.raises(Back):
        cli._choose("Способ оплаты:", ["Картой", "Безнал"], allow_back=True)

    assert BACK_LABEL in capsys.readouterr().out, "пункт возврата должен быть в списке"


def test_choose_without_back_treats_words_as_invalid(monkeypatch):
    """Без allow_back «назад» — не команда: шаг не должен молча откатываться."""
    from lo_api.cli import order as cli

    answers = iter(["назад", "2"])
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert cli._choose("Способ оплаты:", ["Картой", "Безнал"]) == 1


def test_ask_supports_back(monkeypatch):
    from lo_api.cli import order as cli
    from lo_api.cli.steps import Back

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "<")

    with pytest.raises(Back):
        cli._ask("Номер заказа", allow_back=True)

    monkeypatch.setattr("builtins.input", lambda _: "<")
    assert cli._ask("Номер заказа", "по умолчанию") == "<", (
        "без allow_back «<» — обычный ввод, а не команда"
    )


@pytest.mark.parametrize("initial, total", [(0, 1), (2, 5)])
def test_dairy_without_certificate_sets_total(cfg, monkeypatch, initial, total):
    from lo_api.picking import PickingClient, _pick_task, build_plan

    item = {"sku": "00123", "count": total, "countPick": initial, "barcode": "4600000000017",
            "markType": "MILK", "isOsuMarkRequired": True, "isBatchMarkingAssembly": False}
    task, = build_plan({"itemsList": [item]}, resume=True).tasks
    client = PickingClient(cfg)
    calls = []
    monkeypatch.setattr(client, "call", lambda method, params: calls.append((method, params)))
    _pick_task(client, "order", task)
    assert [m for m, _ in calls] == ["sapMarking", "orderUpdate"]
    assert calls[0][1]["sku"] == "00123"
    assert calls[1][1]["order"]["items"][0]["stock_count"] == total


@pytest.mark.parametrize("picked_after_add", [[2, 3], [1]])
def test_dairy_batches_use_mark_for_each_remaining_batch(cfg, monkeypatch, picked_after_add):
    from lo_api.picking import PickerError, PickingClient, _pick_task, build_plan

    item = {"sku": "00123", "count": 3, "countPick": 1, "isBatchMarkingAssembly": True,
            "barcode": "4600000000017"}
    task, = build_plan({"itemsList": [item]}, resume=True).tasks
    calls = []
    counts = iter(picked_after_add)

    def call(method, params, **kwargs):
        calls.append(method)
        if method == "getBatchMarkingInfo":
            return {"markingInfo": {"gtin": "0123", "productionDate": "2026-05-28T00:00:00Z",
                                    "partNumber": "P1"}}
        if method == "sapManufacturer":
            return {"manufacturers": [{"code": "M1"}]}
        if method == "sapOsuMarkingBatchAdd":
            assert kwargs["attempts"] == 1
            assert params == {"orderId": "order", "sku": "123", "manufacturerCode": "M1",
                              "dateOfManufacture": "28.05.2026", "gtin": "0123",
                              "markingPartNumber": "P1"}
            return {"order": {"itemsList": [dict(item, countPick=next(counts))]}}
        return {}

    client = PickingClient(cfg)
    monkeypatch.setattr(client, "call", call)
    if picked_after_add[-1] == 1:
        with pytest.raises(PickerError, match="было собрано 1, стало 1"):
            _pick_task(client, "order", task)
    else:
        _pick_task(client, "order", task)
    assert calls == ["itemUpdateByBarcode"] + [
        "getBatchMarkingInfo", "sapManufacturer", "sapOsuMarkingBatchAdd"
    ] * len(picked_after_add)


def test_assignment_can_precede_visibility_in_picker(cfg, monkeypatch):
    from lo_api.picking import PickingClient

    order_get = PickingClient.order_get
    cli, client, args, state = _assign_setup(cfg, monkeypatch, applied_after=1)
    assigned = {"pickerStaffInfo": {"buyerId": 1, "name": "Сборщик"},
                "courierStaffInfo": {"buyerId": 2, "name": "Курьер"}}
    reason = "error proxy: rpc error: getOrderInfo orderNumber: 123, error: order not found"
    replies = iter([(500, {"error": {"message": reason}}, reason)] * 2
                   + [(200, {"result": {"order": assigned}}, "")])
    posts_at_read = []

    def attempt(method, body, headers):
        posts_at_read.append(state["posts"])
        return next(replies)

    monkeypatch.setattr(client, "_attempt", attempt)
    monkeypatch.setattr(PickingClient, "order_get", order_get)
    cli.ensure_assigned(cfg, client, "123", args)
    assert posts_at_read == [0, 1, 1]
    assert state["posts"] == 1


@pytest.mark.parametrize("pickup,roles,confirmed", [
    (False, ["Пикер", "Курьер"], True),
    (True, ["Пикер"], True),
    (False, ["Пикер"], False),
])
def test_tms_already_assigned(cfg, monkeypatch, pickup, roles, confirmed):
    from types import SimpleNamespace

    from lo_api.tms import TmsClient

    assign = TmsClient.assign
    cli, client, args, _ = _assign_setup(cfg, monkeypatch, applied_after=None)
    monkeypatch.setattr(TmsClient, "assign", assign)
    messages = "".join(f"<li>{r} с таким КПП уже назначен на этот заказ</li>" for r in roles)
    response = SimpleNamespace(text=f'<ul class="errorlist">{messages}</ul>')
    monkeypatch.setattr(TmsClient, "_post", lambda *a, **kw: response)
    checks = []
    monkeypatch.setattr(cli, "_await_assignment", lambda *a: checks.append(1) or [])
    cli.assemble_order(cfg, "4620001" if pickup else "900001", args, client)
    assert checks == ([] if confirmed else [1])

    from lo_api.tms import TmsError

    response.text += '<ul class="errorlist"><li>Курьер без транспорта</li></ul>'
    with pytest.raises(TmsError, match="Курьер без транспорта"):
        cli.assemble_order(cfg, "900001", args, client)


def test_dairy_batch_flag_arrives_at_assembly_start(cfg, monkeypatch):
    from lo_api import picking

    milk = {"sku": "00123", "count": 1, "controlType": "V", "barcode": "4600000000017",
            "isMarkingInGoods": True, "isOsuMarkRequired": True}
    calls = []

    def call(method, params, **kwargs):
        calls.append(method)
        if method == "orderUpdate":
            return {"order": {"itemsList": [{"sku": "123", "isBatchMarkingAssembly": True},
                                            {"sku": "456", "isBatchMarkingAssembly": False}]}}
        if method == "getBatchMarkingInfo":
            return {"markingInfo": {"gtin": "0123", "productionDate": "2026-05-28"}}
        if method == "sapManufacturer":
            return {"manufacturers": [{"code": "M1"}]}
        if method == "sapOsuMarkingBatchAdd":
            return {"order": {"itemsList": [dict(milk, countPick=1)]}}
        return {}

    client = picking.PickingClient(cfg)
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(picking, "_validate_sap", lambda *args: None)
    picking._assemble_items(client, {"itemsList": [milk]}, "order", False, False)
    assert calls == ["orderPaymentCheck", "orderUpdate", "itemUpdateByBarcode",
                     "getBatchMarkingInfo", "sapManufacturer", "sapOsuMarkingBatchAdd"]


@pytest.mark.parametrize("limit,answers,success", [(2, [], True), (1, ["1"], True),
                                                  (1, ["2"], False)])
def test_batch_marking_info_retry_preserves_mark(cfg, monkeypatch, limit, answers, success):
    from lo_api.cli import order as cli
    from lo_api.picker import PickerError
    from lo_api.picking import PickingClient

    cfg.workshift_retry_attempts = limit
    cfg.retry_backoff = 0
    client = PickingClient(cfg)
    if answers:
        client.decisions = cli.AskDecisions()
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        inputs = iter(answers)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    sent = []
    message = "Не удалось получить производственную партию. Повторите попытку позже"

    def attempt(method, body, headers):
        sent.append(body)
        if len(sent) == 1:
            return 200, {"error": {"message": message}}, message
        return 200, {"result": {"markingInfo": {"gtin": "0123"}}}, ""

    monkeypatch.setattr(client, "_attempt", attempt)
    if success:
        assert client.batch_marking_info("order", "mark") == {"gtin": "0123"}
    else:
        with pytest.raises(PickerError, match="получить производственную партию"):
            client.batch_marking_info("order", "mark")
    assert [(b["method"], b["params"]) for b in sent] == [
        ("getBatchMarkingInfo", {"orderId": "order", "marking": "mark"})
    ] * (2 if success else 1)


def test_log_formats_json_bodies():
    from lo_api.http.transport import pretty_body

    body = {"items": [{"name": "Молоко", "marked": True}], "error": None}
    raw = json.dumps(body)
    expected = json.dumps(body, ensure_ascii=False, indent=2)
    for source in (body, raw, raw.encode(), {"request": raw}):
        assert pretty_body(source, None) == expected
    assert pretty_body("Service Unavailable", None) == "Service Unavailable"


@pytest.mark.parametrize("kind", ["simple", "peu"])
def test_guided_partial_quantity_and_validation(cfg, monkeypatch, kind):
    from lo_api.picking import _assemble_items, build_plan, picked_issues

    item = _ask_item("12", "Товар", 3, isMarking=kind == "peu")
    order = _ask_order(item)
    client, sent = _ask_client(cfg, monkeypatch, ["3", "2"])
    monkeypatch.setattr("lo_api.picking._validate_sap", lambda c, o: None)
    _assemble_items(client, order, "order", False, False)
    if kind == "peu":
        assert sum(m == "itemIncreaseByMarking" for m, _ in sent) == 2
    else:
        assert sent[-1][1]["order"]["items"][0]["stock_count"] == 2

    for got in (1, 2, 3):
        item["countPick"] = item["stockData"]["records"][0]["countPick"] = got
        assert bool(picked_issues(order, targets=client.partial_targets)) == (got != 2)
        repair = build_plan(order, resume=True, targets=client.partial_targets)
        assert [t.count for t in repair.tasks] == ([1] if got == 1 else [])


def test_order_update_retries_internal_error_with_http_200(cfg, monkeypatch):
    from lo_api.picking import PickingClient

    calls = []
    cfg.retry_backoff = 0
    cfg.workshift_retry_attempts = 2

    def post(self, url, json, **kwargs):
        calls.append(json)
        if len(calls) == 1:
            return _resp(200, {"error": {"message": "Внутренняя ошибка сервиса."}})
        return _resp(200, {"result": {"updated": True}})

    monkeypatch.setattr(requests.Session, "post", post)
    result = PickingClient(cfg).set_status("123", "PACKAGING")
    assert result == {"updated": True}
    assert [c["method"] for c in calls] == ["orderUpdate", "orderUpdate"]
    assert calls[0]["params"] == calls[1]["params"]
    assert calls[0]["id"] != calls[1]["id"]
