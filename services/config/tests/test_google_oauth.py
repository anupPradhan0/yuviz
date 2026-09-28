"""Google OAuth start/callback. verified_identity is patched, so these cover
the state/cookie binding and account rules, not Google itself."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from services.config import auth, google_oauth
from services.config import users as users_service
from services.config.google_oauth import GoogleIdentity
from services.config.routers.auth import _GOOGLE_COOKIE
from services.config.tests.test_signup import _signup, anon_client, fresh_db, mailer  # noqa: F401


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


async def _callback(client, state: str, email: str, **names: str):
    identity = GoogleIdentity(email, names.get("given_name"), names.get("family_name"))
    with patch.object(google_oauth, "verified_identity", AsyncMock(return_value=identity)):
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
    async def test_state_without_the_matching_cookie_is_rejected(self, fresh_db, anon_client):
        # Login CSRF: an attacker's state replayed in a victim browser that
        # never started the flow has no cookie.
        state = await _start(anon_client, "create")
        anon_client.cookies.clear()
        resp = await _callback(anon_client, state, "admin@example.com")
        assert "token" not in _fragment(resp)
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_forged_state_is_rejected(self, anon_client):
        await _start(anon_client, "create")
        resp = await _callback(anon_client, "not-a-signed-state", "admin@example.com")
        assert "token" not in _fragment(resp)

    async def test_user_cancelling_at_google_returns_an_error(self, anon_client):
        resp = await anon_client.get("/auth/oauth/google/callback?error=access_denied")
        assert "cancelled" in _fragment(resp)["error"]


class TestCreateMode:
    async def test_registers_an_admin_in_a_new_organization(self, fresh_db, anon_client):
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "jane@example.com", given_name="Jane", family_name="Doe")
        user = auth.decode_access_token(_fragment(resp)["token"])
        assert user.email == "jane@example.com"
        assert user.role == "admin"
        assert user.tenant_id is not None
        row = await fresh_db.fetchrow(
            "SELECT u.first_name, u.last_name, t.name FROM users u JOIN tenants t ON t.id = u.tenant_id "
            "WHERE u.email = 'jane@example.com'",
        )
        assert (row["first_name"], row["last_name"], row["name"]) == ("Jane", "Doe", "Jane's organization")

    async def test_never_grants_superadmin_even_on_an_empty_install(self, fresh_db, anon_client):
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "admin@example.com")
        assert auth.decode_access_token(_fragment(resp)["token"]).role == "admin"
        assert await fresh_db.fetchval("SELECT count(*) FROM users WHERE role = 'superadmin'") == 0

    async def test_needs_no_code_and_clears_a_pending_code_signup(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup("jane@example.com"))
        mailer.reset_mock()
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "jane@example.com", given_name="Jane")
        assert auth.decode_access_token(_fragment(resp)["token"]).role == "admin"
        mailer.assert_not_called()
        assert await fresh_db.fetchval("SELECT count(*) FROM pending_registrations") == 0

    async def test_can_set_a_password_without_a_current_one_only_once(self, anon_client):
        state = await _start(anon_client, "create")
        token = _fragment(await _callback(anon_client, state, "jane@example.com"))["token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = await anon_client.get("/auth/me", headers=headers)
        assert me.json()["password_set"] is False

        resp = await anon_client.post(
            "/auth/change-password", json={"new_password": "chosen-password"}, headers=headers,
        )
        assert resp.status_code == 204
        login = await anon_client.post(
            "/auth/login", json={"email": "jane@example.com", "password": "chosen-password"},
        )
        assert login.status_code == 200
        resp = await anon_client.post(
            "/auth/change-password", json={"new_password": "another-password"}, headers=headers,
        )
        assert resp.status_code == 400

    async def test_can_change_email_without_a_current_password(self, anon_client, mailer):
        state = await _start(anon_client, "create")
        token = _fragment(await _callback(anon_client, state, "jane@example.com"))["token"]
        resp = await anon_client.post(
            "/auth/change-email", json={"new_email": "jane@new.com"}, headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202

    async def test_existing_email_is_told_to_sign_in(self, fresh_db, anon_client):
        await users_service.seed_superadmin(email="admin@example.com", password="a-real-password")
        state = await _start(anon_client, "create")
        resp = await _callback(anon_client, state, "admin@example.com")
        assert "already registered" in _fragment(resp)["error"]
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1


class TestSigninMode:
    async def test_existing_user_gets_their_own_token(self, anon_client):
        await users_service.seed_superadmin(email="admin@example.com", password="a-real-password")
        state = await _start(anon_client, "signin")
        resp = await _callback(anon_client, state, "Admin@Example.com")
        assert auth.decode_access_token(_fragment(resp)["token"]).email == "admin@example.com"

    async def test_unknown_email_never_creates_an_account(self, fresh_db, anon_client):
        await users_service.seed_superadmin(email="admin@example.com", password="a-real-password")
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
