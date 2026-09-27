"""Google OAuth sign-in: /auth/oauth/google/start + /auth/oauth/google/callback.

Google itself is never called — verified_email is patched, so these cover
the state/cookie binding and the account rules, not Google's token endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from services.config import auth, google_oauth
from services.config import users as users_service
from services.config.routers.auth import _GOOGLE_COOKIE
from services.config.tests.test_bootstrap import anon_client, fresh_db  # noqa: F401


@pytest.fixture(autouse=True)
def configured():
    with patch.object(google_oauth, "CLIENT_ID", "test-client"), \
         patch.object(google_oauth, "CLIENT_SECRET", "test-secret"):
        yield


def _fragment(resp) -> dict[str, str]:
    location = resp.headers["location"]
    assert location.startswith(f"{google_oauth.ADMIN_UI_URL}/login#")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).fragment).items()}


async def _start(client, mode: str) -> str:
    """Runs /start and returns the state; the client keeps the nonce cookie."""
    resp = await client.get(f"/auth/oauth/google/start?mode={mode}")
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]


async def _callback(client, state: str, email: str):
    with patch.object(google_oauth, "verified_email", AsyncMock(return_value=email)):
        return await client.get(f"/auth/oauth/google/callback?code=c&state={state}")


class TestStart:
    async def test_redirects_to_google_with_state_and_sets_nonce_cookie(self, anon_client):
        resp = await anon_client.get("/auth/oauth/google/start?mode=signin")
        assert resp.status_code == 302
        url = urlsplit(resp.headers["location"])
        assert url.netloc == "accounts.google.com"
        query = parse_qs(url.query)
        assert query["client_id"] == ["test-client"]
        assert query["redirect_uri"] == [google_oauth.REDIRECT_URI]
        assert _GOOGLE_COOKIE in resp.cookies
        assert "httponly" in resp.headers["set-cookie"].lower()

    async def test_not_configured_bounces_back_with_error(self, anon_client):
        with patch.object(google_oauth, "CLIENT_ID", ""):
            resp = await anon_client.get("/auth/oauth/google/start")
        assert "not configured" in _fragment(resp)["error"]


class TestCallbackStateBinding:
    async def test_state_without_the_matching_cookie_is_rejected(self, anon_client):
        # Login CSRF: an attacker's state replayed in a victim browser that
        # never started the flow has no cookie.
        state = await _start(anon_client, "create")
        anon_client.cookies.clear()
        resp = await _callback(anon_client, state, "admin@example.com")
        assert "token" not in _fragment(resp)
        assert await users_service.superadmin_exists() is False

    async def test_forged_state_is_rejected(self, anon_client):
        await _start(anon_client, "create")
        resp = await _callback(anon_client, "not-a-signed-state", "admin@example.com")
        assert "token" not in _fragment(resp)

    async def test_user_cancelling_at_google_returns_an_error(self, anon_client):
        resp = await anon_client.get("/auth/oauth/google/callback?error=access_denied")
        assert "cancelled" in _fragment(resp)["error"]


class TestCreateMode:
    async def test_bootstraps_the_first_superadmin(self, anon_client):
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "admin@example.com")
        user = auth.decode_access_token(_fragment(resp)["token"])
        assert user.email == "admin@example.com"
        assert user.role == "superadmin"
        assert user.tenant_id is None

    async def test_rejected_once_setup_is_done(self, fresh_db, anon_client):
        await users_service.bootstrap_first_superadmin(email="first@example.com", password="a-real-password")
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "second@example.com")
        assert "already been completed" in _fragment(resp)["error"]
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1


class TestSigninMode:
    async def test_existing_user_gets_their_own_token(self, anon_client):
        await users_service.bootstrap_first_superadmin(email="admin@example.com", password="a-real-password")
        state = await _start(anon_client, "signin")
        resp = await _callback(anon_client, state, "Admin@Example.com")
        assert auth.decode_access_token(_fragment(resp)["token"]).email == "admin@example.com"

    async def test_unknown_email_never_creates_an_account(self, fresh_db, anon_client):
        await users_service.bootstrap_first_superadmin(email="admin@example.com", password="a-real-password")
        state = await _start(anon_client, "signin")
        resp = await _callback(anon_client, state, "stranger@example.com")
        assert "No account exists" in _fragment(resp)["error"]
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1

    async def test_tenant_user_keeps_their_tenant_scope(self, fresh_db, anon_client):
        tenant_id = await fresh_db.fetchval(
            "INSERT INTO tenants (name, slug) VALUES ('Acme', 'acme') RETURNING id",
        )
        await users_service.create_user(
            email="viewer@acme.com", password="a-real-password", role="viewer", tenant_id=tenant_id,
        )
        state = await _start(anon_client, "signin")
        resp = await _callback(anon_client, state, "viewer@acme.com")
        user = auth.decode_access_token(_fragment(resp)["token"])
        assert user.role == "viewer"
        assert user.tenant_id == str(tenant_id)
