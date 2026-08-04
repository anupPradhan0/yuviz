"""
Tests the actual HTTP layer (routing, request validation, status codes, error
mapping) in-process via httpx's ASGITransport — no live uvicorn process, but
every request really goes through FastAPI's routing/validation and really
hits Postgres + Redis (same conftest fixtures as the service-layer tests).
FastAPI's lifespan isn't triggered here since it only eagerly warms the same
lazy singletons db.py/cache.py already create on first use — nothing this
test needs depends on the lifespan hook specifically running.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from services.config.app import app


@pytest.fixture
async def client(test_superadmin):
    # Pre-authenticated as superadmin by default — most of this file's tests
    # predate real auth and are about routing/validation/status codes, not
    # authorization itself; TestAuthEndpoints below covers login/401/403
    # explicitly with its own unauthenticated/role-restricted clients.
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_superadmin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
async def anon_client():
    """No Authorization header at all — for asserting 401s."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def viewer_client(test_viewer):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_viewer['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


class TestTenantEndpoints:
    async def test_create_and_get_tenant(self, client):
        slug = f"test-{uuid.uuid4().hex[:8]}"
        try:
            resp = await client.post("/tenants", json={"name": "API Test", "slug": slug})
            assert resp.status_code == 201
            body = resp.json()
            assert body["slug"] == slug
            assert body["config_version"] == 1

            resp = await client.get(f"/tenants/{slug}")
            assert resp.status_code == 200
            assert resp.json()["slug"] == slug
        finally:
            from services.config import cache, db
            pool = await db.get_pool()
            await pool.execute("DELETE FROM tenants WHERE slug = $1", slug)
            await cache.invalidate(f"tenant:{slug}")

    async def test_get_unknown_tenant_returns_404(self, client):
        resp = await client.get("/tenants/does-not-exist")
        assert resp.status_code == 404

    async def test_update_tenant_via_patch(self, client, test_tenant):
        resp = await client.patch(f"/tenants/{test_tenant['id']}", json={"vad_hold_ms": 700})
        assert resp.status_code == 200
        assert resp.json()["vad_hold_ms"] == 700
        assert resp.json()["config_version"] == test_tenant["config_version"] + 1

    async def test_update_tenant_with_empty_body_is_400(self, client, test_tenant):
        resp = await client.patch(f"/tenants/{test_tenant['id']}", json={})
        assert resp.status_code == 400

    async def test_update_tenant_unknown_field_is_422(self, client, test_tenant):
        # Pydantic rejects an unrecognized field before it ever reaches
        # tenants_service.update_tenant()'s own ValueError check.
        resp = await client.patch(
            f"/tenants/{test_tenant['id']}", json={"not_a_real_field": "x"},
        )
        assert resp.status_code in (400, 422)

    async def test_delete_tenant(self, client, pool):
        create = await client.post(
            "/tenants", json={"name": "Delete Me", "slug": f"test-del-{uuid.uuid4().hex[:8]}"},
        )
        tenant = create.json()
        resp = await client.delete(f"/tenants/{tenant['id']}")
        assert resp.status_code == 204

        resp = await client.get(f"/tenants/{tenant['slug']}")
        assert resp.status_code == 404  # soft-deleted, excluded from reads

        await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


class TestAgentEndpoints:
    async def test_create_and_get_agent(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "greeting": "Hi!"},
        )
        assert resp.status_code == 201
        assert resp.json()["slug"] == "support-agent"

        resp = await client.get(f"/tenants/{test_tenant['slug']}/agents/support-agent")
        assert resp.status_code == 200
        assert resp.json()["greeting"] == "Hi!"

    async def test_create_agent_under_unknown_tenant_is_404(self, client):
        resp = await client.post(
            "/tenants/no-such-tenant/agents", json={"slug": "x", "name": "X"},
        )
        assert resp.status_code == 404

    async def test_update_agent_transfer_config(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]

        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"transfer_type": "warm", "transfer_destination": "+18005550100"},
        )
        assert resp.status_code == 200
        assert resp.json()["transfer_type"] == "warm"

    async def test_update_agent_invalid_transfer_type_is_422(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]

        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"transfer_type": "not-a-real-type"},
        )
        assert resp.status_code == 422  # Pydantic Literal validation


class TestProviderConfigEndpoints:
    async def test_create_list_and_filter(self, client, test_tenant):
        await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram", "environment": "prod"},
        )
        await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Whisper", "role": "stt", "engine": "faster_whisper", "environment": "dev"},
        )

        resp = await client.get(f"/tenants/{test_tenant['id']}/providers", params={"role": "stt"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        resp = await client.get(
            f"/tenants/{test_tenant['id']}/providers",
            params={"role": "stt", "environment": "prod"},
        )
        assert [p["engine"] for p in resp.json()] == ["deepgram"]

    async def test_nonexistent_tenant_id_is_404_not_500(self, client):
        """Regression test: a well-formed but nonexistent tenant_id used to
        hit an unhandled ForeignKeyViolationError and return a bare 500 with
        Postgres internals in the traceback."""
        resp = await client.post(
            "/tenants/00000000-0000-0000-0000-000000000000/providers",
            json={"name": "X", "role": "stt", "engine": "deepgram"},
        )
        assert resp.status_code == 404

    async def test_malformed_tenant_id_is_400_not_500(self, client):
        resp = await client.post(
            "/tenants/not-a-uuid/providers",
            json={"name": "X", "role": "stt", "engine": "deepgram"},
        )
        assert resp.status_code == 400

    async def test_invalid_role_is_422(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Bad", "role": "not-a-role", "engine": "x"},
        )
        assert resp.status_code == 422

    async def test_get_update_delete_provider_config(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = create.json()["id"]

        resp = await client.get(f"/providers/{provider_id}")
        assert resp.status_code == 200

        resp = await client.patch(f"/providers/{provider_id}", json={"model": "nova-3-medical"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "nova-3-medical"

        resp = await client.delete(f"/providers/{provider_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/providers/{provider_id}")
        assert resp.status_code == 404


class TestToolProviderConfigEndpoints:
    """Regression coverage for the blank api_key_ref gap (2026-07-23): a
    tool_provider_config with no key silently passed creation and only
    failed at call time (provider_manager.py's _make_cal_com), which broke
    a live call. Now caught here instead."""

    async def test_create_with_valid_api_key_ref(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "Book Appointment (Cal.com)", "tool_name": "book_appointment",
                "engine": "cal_com", "api_key_ref": "env:CAL_API_KEY", "extra": {"event_type_id": 123},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["api_key_ref"] == "env:CAL_API_KEY"

    async def test_create_with_missing_api_key_ref_is_422(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "X", "tool_name": "book_appointment", "engine": "cal_com"},
        )
        assert resp.status_code == 422

    async def test_create_with_blank_api_key_ref_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "X", "tool_name": "book_appointment", "engine": "cal_com", "api_key_ref": "   "},
        )
        assert resp.status_code == 400

    async def test_update_to_blank_api_key_ref_is_400(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "X", "tool_name": "book_appointment", "engine": "cal_com",
                "api_key_ref": "env:CAL_API_KEY",
            },
        )
        tpc_id = create.json()["id"]

        resp = await client.patch(f"/tool-providers/{tpc_id}", json={"api_key_ref": ""})
        assert resp.status_code == 400


class TestCarrierEndpoints:
    """DID Management platform (2026-07-23): carriers previously had no
    CRUD/router at all, only an existence-check helper used by
    phone_numbers' own validation."""

    async def test_create_list_and_get_carrier(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={
                "name": "Plivo Main", "provider": "plivo",
                "auth_id": "MAXXXXXXXXXXXXXXXXXX", "auth_token_ref": "env:PLIVO_AUTH_TOKEN",
                "carrier_account_ref": "MAXXXXXXXXXXXXXXXXXX",
            },
        )
        assert create.status_code == 201
        body = create.json()
        assert body["provider"] == "plivo"
        assert body["auth_token_ref"] == "env:PLIVO_AUTH_TOKEN"
        carrier_id = body["id"]

        resp = await client.get(f"/tenants/{test_tenant['id']}/carriers")
        assert resp.status_code == 200
        assert [c["id"] for c in resp.json()] == [carrier_id]

        resp = await client.get(f"/carriers/{carrier_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Plivo Main"

    async def test_invalid_provider_is_422(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={"name": "Bad", "provider": "not-a-real-carrier"},
        )
        assert resp.status_code == 422

    async def test_nonexistent_tenant_id_is_404_not_500(self, client):
        resp = await client.post(
            "/tenants/00000000-0000-0000-0000-000000000000/carriers",
            json={"name": "X", "provider": "plivo"},
        )
        assert resp.status_code == 404

    async def test_update_and_delete_carrier(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={"name": "Plivo Main", "provider": "plivo"},
        )
        carrier_id = create.json()["id"]

        resp = await client.patch(f"/carriers/{carrier_id}", json={"name": "Plivo Renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Plivo Renamed"

        resp = await client.delete(f"/carriers/{carrier_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/carriers/{carrier_id}")
        assert resp.status_code == 404


class TestPhoneNumberEndpoints:
    async def test_create_with_nonexistent_carrier_id_is_404_not_400(self, client, test_tenant):
        """Regression test: carrier_id used to be unvalidated, so a bad value
        only surfaced via the app-wide FK-violation-to-400 handler — a
        precise 404 (matching agent_id/fallback_agent_id's own behavior)
        instead of a generic 400."""
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={
                "did": f"test-did-{uuid.uuid4().hex[:8]}",
                "carrier_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert resp.status_code == 404
        assert "carrier" in resp.json()["detail"]

    async def test_create_with_malformed_carrier_id_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={"did": f"test-did-{uuid.uuid4().hex[:8]}", "carrier_id": "not-a-uuid"},
        )
        assert resp.status_code == 400

    async def test_update_with_nonexistent_carrier_id_is_404(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={"did": f"test-did-{uuid.uuid4().hex[:8]}"},
        )
        phone_number_id = create.json()["id"]

        resp = await client.patch(
            f"/phone-numbers/{phone_number_id}",
            json={"carrier_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert resp.status_code == 404
        assert "carrier" in resp.json()["detail"]


class TestCallEndpoints:
    async def test_list_calls_for_unknown_tenant_is_404(self, client):
        resp = await client.get("/tenants/not-a-real-tenant-slug/calls")
        assert resp.status_code == 404

    async def test_list_and_get_call(self, client, test_tenant, pool):
        session_id = f"test-call-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
            session_id, test_tenant["slug"],
        )

        resp = await client.get(f"/tenants/{test_tenant['slug']}/calls")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["session_id"] == session_id
        assert body["items"][0]["mode"] == "AI"

        resp = await client.get(f"/calls/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id

        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)

    async def test_get_unknown_call_is_404(self, client):
        resp = await client.get("/calls/does-not-exist")
        assert resp.status_code == 404

    async def test_get_transcript_for_unknown_call_is_404(self, client):
        resp = await client.get("/calls/does-not-exist/transcript")
        assert resp.status_code == 404

    async def test_get_transcript(self, client, test_tenant, pool):
        session_id = f"test-call-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
            session_id, test_tenant["slug"],
        )
        await pool.execute(
            "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
            "VALUES ($1, 1, 'hi', 'hello')",
            session_id,
        )

        resp = await client.get(f"/calls/{session_id}/transcript")
        assert resp.status_code == 200
        assert resp.json()[0]["caller_text"] == "hi"

        await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


class TestAuthEndpoints:
    async def test_login_succeeds_with_correct_credentials(self, anon_client, test_superadmin):
        resp = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "test-password-not-real"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert "access_token" in body
        assert body["user"]["email"] == test_superadmin["user"]["email"]
        assert "password_hash" not in body["user"]

    async def test_login_fails_with_wrong_password(self, anon_client, test_superadmin):
        resp = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "wrong-password"},
        )
        assert resp.status_code == 401

    async def test_login_fails_for_unknown_email(self, anon_client):
        resp = await anon_client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "anything"},
        )
        assert resp.status_code == 401

    async def test_me_requires_auth(self, anon_client):
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_returns_current_user(self, client, test_superadmin):
        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == test_superadmin["user"]["email"]

    async def test_protected_endpoint_without_token_is_401(self, anon_client):
        resp = await anon_client.get("/tenants")
        assert resp.status_code == 401

    async def test_protected_endpoint_with_malformed_header_is_401(self, anon_client):
        resp = await anon_client.get("/tenants", headers={"Authorization": "not-a-bearer-token"})
        assert resp.status_code == 401

    async def test_viewer_can_read_but_not_write(self, viewer_client, test_tenant):
        get_resp = await viewer_client.get(f"/tenants/{test_tenant['slug']}")
        assert get_resp.status_code == 200

        patch_resp = await viewer_client.patch(f"/tenants/{test_tenant['id']}", json={"name": "Hijacked"})
        assert patch_resp.status_code == 403

    async def test_change_password_requires_auth(self, anon_client):
        resp = await anon_client.post(
            "/auth/change-password", json={"current_password": "x", "new_password": "newpassword123"},
        )
        assert resp.status_code == 401

    async def test_change_password_succeeds_and_old_password_stops_working(self, client, anon_client, test_superadmin):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "test-password-not-real", "new_password": "brand-new-password"},
        )
        assert resp.status_code == 204

        old_login = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "test-password-not-real"},
        )
        assert old_login.status_code == 401

        new_login = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "brand-new-password"},
        )
        assert new_login.status_code == 200

    async def test_change_password_fails_with_wrong_current_password(self, client):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "totally-wrong", "new_password": "brand-new-password"},
        )
        assert resp.status_code == 400

    async def test_change_password_rejects_too_short_new_password(self, client):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "test-password-not-real", "new_password": "short"},
        )
        assert resp.status_code == 422


class TestUserEndpoints:
    async def test_create_user_requires_superadmin_or_admin(self, viewer_client):
        resp = await viewer_client.post(
            "/users", json={"email": "new@example.com", "password": "pw", "role": "viewer"},
        )
        assert resp.status_code == 403

    async def test_superadmin_can_create_list_and_delete_user(self, client):
        email = f"test-created-{uuid.uuid4().hex[:8]}@example.com"
        create_resp = await client.post(
            "/users", json={"email": email, "password": "a-real-password", "role": "admin"},
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        assert body["email"] == email
        assert "password_hash" not in body

        list_resp = await client.get("/users")
        assert any(u["email"] == email for u in list_resp.json())

        del_resp = await client.delete(f"/users/{body['id']}")
        assert del_resp.status_code == 204


class TestHealthEndpoint:
    async def test_health_check(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
