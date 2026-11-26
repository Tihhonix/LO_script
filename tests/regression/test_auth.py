"""Регресс авторизации и выбора организации.

Логин выполняется один раз на прогон (фикстура `authorized_api`): стенд
лимитирует частоту запросов кода входа, поэтому тесты проверяют результат
авторизации, а не повторяют её.
"""

from __future__ import annotations

import pytest

from lo_api.clients import LentaApi

pytestmark = pytest.mark.regression


def test_login_fills_context(authorized_api: LentaApi, account):
    assert authorized_api.ctx.session_token, "после login в контексте нет sessionToken"
    assert authorized_api.ctx.passport_id, "после login в контексте нет passportId"
    assert authorized_api.ctx.authorized


def test_profile_available_after_login(authorized_api: LentaApi):
    profile = authorized_api.user.profile()

    assert profile, "users/profile вернул пустой ответ"


def test_organizations_available(authorized_api: LentaApi, account):
    orgs = authorized_api.b2b.organizations()

    assert orgs, f"у аккаунта {account} нет организаций"
    for org in orgs:
        assert org.legal_entity_id, f"у организации {org.name} нет legalEntityId"
        assert org.inn.isdigit() and len(org.inn) in (10, 12), (
            f"некорректный ИНН у {org.name}: {org.inn!r}"
        )


def test_selected_organization_lands_in_context(authorized_api: LentaApi):
    """Регресс на найденный баг: раньше x-organization-id был захардкожен,
    и выбор организации ни на что не влиял."""
    orgs = authorized_api.b2b.organizations()
    original = authorized_api.ctx.legal_entity_id
    target = orgs[-1]

    try:
        authorized_api.b2b.select(target)

        assert authorized_api.ctx.legal_entity_id == target.legal_entity_id
        headers = authorized_api.t._dynamic_headers()
        assert headers["x-organization-id"] == target.legal_entity_id
        assert headers["x-passport-id"] == authorized_api.ctx.passport_id
    finally:
        # сессия общая — возвращаем организацию, с которой работают другие тесты
        restore = next((o for o in orgs if o.legal_entity_id == original), orgs[0])
        authorized_api.b2b.select(restore)
