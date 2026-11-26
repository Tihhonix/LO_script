"""TMS Lite — назначение сборщика и курьера на созданный заказ.

Отдельная система со своим протоколом: Django-админка с HTML-формами,
сессионными cookie и CSRF-токеном, а не JSON API Ленты. Поэтому здесь своя
`requests.Session` с куками — запрет на `Cookie` из `http/transport.py`
относится к API Ленты, где достаточно `Head.SessionToken`, и на TMS
не распространяется.

Флоу подтверждён HAR-записью назначения:
    GET  /admin/login/?next=/investigate/        — форма, из неё CSRF
    POST /admin/login/                           — вход, 302 на /investigate/
    POST /investigate/transfer_order             — 302 на страницу заказа
    GET  /investigate/transfer_order/<id>        — форма с исполнителями
    POST /investigate/transfer_order/<id>        — собственно назначение
"""

from __future__ import annotations

import html
import logging
import re

import requests

from lo_api.config import Settings
from lo_api.http.errors import LoApiError, network_error_text

log = logging.getLogger("lo_api.tms")

# Значение кнопки отправки. Из формы берётся фактическое, это лишь запасное.
SUBMIT_ACTION = "Сохранить изменения"

# Заглушки в списках исполнителей: на стенде их три десятка, а настоящий
# сборщик и курьер — по одному. Выбирать вслепую первый вариант нельзя.
DUMMY_MARKER = "dummy"


class TmsError(LoApiError):
    """Ошибка обращения к TMS Lite."""


class Performer:
    """Исполнитель — сборщик или курьер — из выпадающего списка формы."""

    def __init__(self, id: str, title: str) -> None:
        self.id = id
        self.title = title

    @property
    def is_dummy(self) -> bool:
        return DUMMY_MARKER in self.title.lower()

    def __str__(self) -> str:
        return self.title or self.id


class OrderPage:
    """Форма назначения: CSRF, значение кнопки и списки исполнителей."""

    def __init__(self, csrf: str, action: str,
                 pickers: list[Performer], couriers: list[Performer]) -> None:
        self.csrf = csrf
        self.action = action
        self.pickers = pickers
        self.couriers = couriers

    @staticmethod
    def _real_first(performers: list[Performer]) -> list[Performer]:
        """Настоящие исполнители впереди заглушек, порядок внутри сохраняется."""
        return sorted(performers, key=lambda p: p.is_dummy)

    @property
    def picker_choices(self) -> list[Performer]:
        return self._real_first(self.pickers)

    @property
    def courier_choices(self) -> list[Performer]:
        return self._real_first(self.couriers)

    @property
    def real_pickers(self) -> list[Performer]:
        return [p for p in self.pickers if not p.is_dummy]

    @property
    def real_couriers(self) -> list[Performer]:
        return [c for c in self.couriers if not c.is_dummy]

    @property
    def has_real_performers(self) -> bool:
        """Есть ли кого назначить кроме заглушек.

        Сотрудник появляется в списке, только когда открыл смену. Сплошные
        Dummy означают, что на смену никто не выведен, — назначать некого.
        Самовывозу хватает одного сборщика, это решается на уровне сценария.
        """
        return bool(self.real_pickers and self.real_couriers)

    def summary(self) -> str:
        """Что форма TMS реально предлагает — для журнала и текста ошибки."""
        def part(title: str, all_: list[Performer], real: list[Performer]) -> str:
            found = ", ".join(str(p) for p in real) if real else "только заглушки"
            return f"{title}: вариантов {len(all_)}, настоящих {len(real)} ({found})"
        return (part("сборщики", self.pickers, self.real_pickers) + "; "
                + part("курьеры", self.couriers, self.real_couriers))


def _plain(fragment: str) -> str:
    """Текст из куска HTML: без тегов, сущности раскрыты, пробелы схлопнуты."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text).strip()


def form_notes(page: str) -> list[str]:
    """Что TMS сообщил после отправки формы: сообщения Django и ошибки полей.

    Без этого «назначено» от «не принято» не отличить: оба ответа — 200.
    """
    notes = []
    kinds = {"success": "успех", "info": "сообщение", "warning": "предупреждение",
             "error": "ошибка"}
    for block in re.findall(r'<ul[^>]*class="[^"]*messagelist[^"]*"[^>]*>(.*?)</ul>',
                            page, re.S):
        for level, text in re.findall(r'<li[^>]*class="([a-z]+)[^"]*"[^>]*>(.*?)</li>',
                                      block, re.S):
            clean = _plain(text)
            if clean:
                notes.append(f"{kinds.get(level, level)}: {clean}")
    for block in re.findall(r'<ul[^>]*class="[^"]*errorlist[^"]*"[^>]*>(.*?)</ul>', page, re.S):
        for text in re.findall(r"<li[^>]*>(.*?)</li>", block, re.S):
            clean = _plain(text)
            if clean:
                notes.append(f"ошибка: {clean}")
    return notes


_PICKER_ASSIGNED = "Пикер с таким КПП уже назначен на этот заказ"
_COURIER_ASSIGNED = "Курьер с таким КПП уже назначен на этот заказ"


def assignment_confirmed(notes: list[str], *, needs_courier: bool) -> bool:
    return _PICKER_ASSIGNED in notes and (not needs_courier or _COURIER_ASSIGNED in notes)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', page)
    if not match:
        raise TmsError("tms/csrf",
                       "на странице нет csrfmiddlewaretoken — вероятно, вместо формы "
                       "пришла страница входа: проверьте LO_TMS_USER и LO_TMS_PASSWORD")
    return match.group(1)


def _options(page: str, field: str) -> list[Performer]:
    block = re.search(rf'<select[^>]*name="{field}"[^>]*>(.*?)</select>', page, re.S)
    if not block:
        return []
    found = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', block.group(1), re.S)
    return [Performer(value, html.unescape(re.sub(r"\s+", " ", title)).strip())
            for value, title in found if value]


def _submit_action(page: str) -> str:
    match = re.search(r'name="action"[^>]*value="([^"]*)"', page)
    return html.unescape(match.group(1)) if match else SUBMIT_ACTION


class TmsClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.base = cfg.tms_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["user-agent"] = cfg.user_agent

    # -- низкий уровень ----------------------------------------------------
    def _get(self, path: str, **kw) -> requests.Response:
        return self._request("GET", path, **kw)

    def _post(self, path: str, data: dict, **kw) -> requests.Response:
        # Referer обязателен: Django отвергает POST без него как CSRF-атаку
        headers = {"referer": f"{self.base}{path}"}
        return self._request("POST", path, data=data, headers=headers, **kw)

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        url = f"{self.base}{path}"
        kw.setdefault("timeout", self.cfg.timeout)
        try:
            resp = self.session.request(method, url, **kw)
        except requests.RequestException as exc:
            raise TmsError(f"tms {path}", network_error_text(exc, self.cfg.tms_url)) from exc
        log.info("%-46s %s %s → %s", path[:46], method, url, resp.status_code)
        if resp.status_code >= 400:
            raise TmsError(f"tms {path}",
                           f"HTTP {resp.status_code}. Тело: {resp.text[:300]}",
                           status=resp.status_code)
        return resp

    # -- шаги --------------------------------------------------------------
    def login(self) -> None:
        """Вход в админку. Сессия и CSRF дальше живут в cookie."""
        if not self.cfg.tms_user or not self.cfg.tms_password:
            raise TmsError(
                "tms/login",
                "не заданы учётные данные TMS. Пропишите LO_TMS_USER и "
                "LO_TMS_PASSWORD в .env — без них назначение недоступно",
            )
        path = "/admin/login/?next=/investigate/"
        form = self._get(path)
        self._post(path, {
            "csrfmiddlewaretoken": _csrf(form.text),
            "username": self.cfg.tms_user,
            "password": self.cfg.tms_password,
            "next": "/investigate/",
        })
        # Django возвращает форму входа снова, если пароль неверен
        if "/admin/login" in self.session.get(
            f"{self.base}/investigate/", timeout=self.cfg.timeout, allow_redirects=False
        ).headers.get("location", ""):
            raise TmsError("tms/login",
                           f"вход отклонён для пользователя {self.cfg.tms_user!r}: "
                           f"проверьте LO_TMS_USER и LO_TMS_PASSWORD")
        log.info("TMS: вход выполнен под %s", self.cfg.tms_user)

    def open_order(self, order_id: str, delivery_type: str | None = None) -> OrderPage:
        """Открывает заказ в расследовании и возвращает форму назначения."""
        delivery_type = delivery_type or self.cfg.tms_delivery_type
        search = self._get("/investigate/transfer_order")
        self._post("/investigate/transfer_order", {
            "csrfmiddlewaretoken": _csrf(search.text),
            "delivery_type": delivery_type,
            "reserve_key": str(order_id),
        })
        page = self._get(
            f"/investigate/transfer_order/{order_id}?delivery_type={delivery_type}"
        )
        form = OrderPage(
            csrf=_csrf(page.text),
            action=_submit_action(page.text),
            pickers=_options(page.text, "new_picker"),
            couriers=_options(page.text, "new_couriers"),
        )
        # Без этой строки из журнала не понять, почему назначать «некого»:
        # то ли смену никто не открыл, то ли заказ относится к другому хабу
        log.info("TMS: форма заказа %s — %s", order_id, form.summary())
        return form

    def assign(self, order_id: str, page: OrderPage, *,
               picker: Performer, courier: Performer | None = None,
               delivery_type: str | None = None) -> list[str]:
        """Назначает сборщика и, если он нужен, курьера. Возвращает ответ формы.

        `courier=None` — это вариант «Не переносить» из самой формы: у
        самовывоза курьера нет, и требовать его значит не назначить заказ.
        """
        delivery_type = delivery_type or self.cfg.tms_delivery_type
        resp = self._post(
            f"/investigate/transfer_order/{order_id}?delivery_type={delivery_type}",
            {
                "csrfmiddlewaretoken": page.csrf,
                "picker_field_choices": self.cfg.tms_picker_mode,
                "new_picker": picker.id,
                "new_picker_instant": picker.id,
                "couriers_field_choices": self.cfg.tms_courier_mode if courier else "",
                "new_couriers": courier.id if courier else "",
                "new_taxi": self.cfg.tms_taxi_mode,
                "action": page.action,
            },
        )
        # 200 ещё не значит «назначено»: форма Django отвечает 200 и при ошибке
        # валидации, а на стенде назначение регулярно не применяется молча.
        # Что TMS сказал — в журнал; факт назначения проверяет вызывающий
        notes = form_notes(resp.text)
        for index, note in enumerate(notes):
            _, _, message = note.partition(": ")
            message = message.rstrip(".")
            if message in (_PICKER_ASSIGNED, _COURIER_ASSIGNED):
                notes[index] = message
            log.info("TMS: %s", notes[index])
        errors = [n for n in notes if n.startswith(("ошибка", "предупреждение"))]
        if errors:
            raise TmsError(f"tms assign {order_id}",
                           "TMS не принял назначение: " + "; ".join(errors))
        log.info("TMS: форма назначения отправлена — сборщик %s, курьер %s",
                 picker, courier or "не требуется")
        return notes

    def close(self) -> None:
        self.session.close()
