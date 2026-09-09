"""Draft-session auth gates for webcall (?draft=1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from services.webcall.__main__ import _CONSOLE_ROLES, _draft_auth_problem

SECRET = "dev-only-insecure-secret-do-not-deploy-dev-only-insecure-secret-do-not-deploy-"


def _tok(**extra) -> str:
    payload = {
        "sub": "u1",
        "email": "a@b.c",
        "role": "admin",
        "tenant_id": "t-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "is_service_account": False,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        **extra,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)


@pytest.mark.asyncio
async def test_console_roles_only():
    assert _CONSOLE_ROLES == frozenset({"superadmin", "admin", "viewer"})
    with patch("services.webcall.__main__._config_tenant_allowed", new_callable=AsyncMock) as allowed:
        allowed.return_value = True
        for role in ("admin", "viewer", "superadmin"):
            assert await _draft_auth_problem(_tok(role=role), "acme") is None
        for role in ("agent", "supervisor"):
            err = await _draft_auth_problem(_tok(role=role), "acme")
            assert err and "not allowed" in err


@pytest.mark.asyncio
async def test_rejects_when_config_denies_tenant():
    token = _tok()
    with patch("services.webcall.__main__._config_tenant_allowed", new_callable=AsyncMock) as allowed:
        allowed.return_value = False
        err = await _draft_auth_problem(token, "other-tenant")
        assert err and "tenant" in err
        allowed.assert_awaited_once_with(token, "other-tenant")


@pytest.mark.asyncio
async def test_rejects_missing_and_service_account():
    assert await _draft_auth_problem(None, "acme")
    assert await _draft_auth_problem(_tok(is_service_account=True), "acme")
