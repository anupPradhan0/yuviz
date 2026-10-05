"""
Route-level tests for the OAuth connector surface: role gating, tenant
scoping, the constant failure and 404 bodies, and the response key set. Real
Postgres, a real signed JWT per user (get_current_user re-reads `users`, so
each principal is a real row), and a recording provider transport.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_target_tenant
from services.config import auth
from services.toolexec import custom_apis, oauth
from services.toolexec.app import app
from services.toolexec.routers import oauth_connections
from services.toolexec.schemas import OAuthAuthorizeRequest, OAuthCallbackRequest

CONNECTION_KEYS = {"id", "provider", "status", "account_label", "scopes", "updated_at"}


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_OAUTH_REDIRECT_URI", "https://console.test/integrations/callback")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")
    monkeypatch.setenv("TOOLEXEC_TEST_GOOGLE_SECRET", "google-client-secret")

    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            "id_token": "h.eyJlbWFpbCI6ICJhQGIuYyIsICJzdWIiOiAiczEifQ.s",
        })

    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(_handler))


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool):
    """Tenants A and B, each with an admin, plus a viewer and a supervisor in A."""
    tenants = {}
    for name in ("a", "b"):
        slug = f"oauthr-{name}-{uuid.uuid4().hex[:8]}"
        tenants[name] = str((await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug))["id"])
    users = {}
    for key, tenant, role in (("admin_a", "a", "admin"), ("admin_a2", "a", "admin"), ("admin_b", "b", "admin"),
                              ("viewer_a", "a", "viewer"), ("supervisor_a", "a", "supervisor")):
        row = await pool.fetchrow(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', $3) "
            "RETURNING id, email, role, tenant_id",
            tenants[tenant], f"{key}-{uuid.uuid4().hex[:8]}@oauthr.test", role,
        )
        token = auth.create_access_token({
            "id": str(row["id"]), "email": row["email"], "role": row["role"],
            "tenant_id": str(row["tenant_id"]), "is_service_account": False,
        })
        users[key] = {"id": str(row["id"]), "headers": {"Authorization": f"Bearer {token}"}}
    yield tenants, users
    set_target_tenant(None)
    ids = list(tenants.values())
    await pool.execute("DELETE FROM oauth_authorization_states WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute(
        "DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE tenant_id = ANY($1::uuid[]))", ids,
    )
    await pool.execute("DELETE FROM users WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _authorize(c, tenant, headers) -> str:
    r = await c.post(f"/tenants/{tenant}/oauth-connections/google/authorize", headers=headers, json={})
    assert r.status_code == 200, r.text
    return parse_qs(urlsplit(r.json()["authorize_url"]).query)["state"][0]


async def _connect(c, tenant, headers) -> dict:
    state = await _authorize(c, tenant, headers)
    r = await c.post(
        f"/tenants/{tenant}/oauth-connections/callback", headers=headers, json={"state": state, "code": "c"},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── disabled by default ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_providers_list_is_empty_until_the_platform_configures_one(world, monkeypatch):
    _tenants, users = world
    async with _client() as c:
        r = await c.get("/oauth-providers", headers=users["admin_a"]["headers"])
        assert r.json() == [{"key": "google", "label": "Google"}]

        monkeypatch.delenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID")
        r = await c.get("/oauth-providers", headers=users["admin_a"]["headers"])
        assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_authorize_is_refused_for_an_unconfigured_provider(world, monkeypatch):
    tenants, users = world
    monkeypatch.delenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID")
    async with _client() as c:
        r = await c.post(
            f"/tenants/{tenants['a']}/oauth-connections/google/authorize",
            headers=users["admin_a"]["headers"], json={},
        )
    assert r.status_code == 400 and r.json() == {"detail": "oauth_provider_unavailable"}


@pytest.mark.asyncio
async def test_every_route_requires_authentication(world):
    tenants, _users = world
    async with _client() as c:
        for method, path in (
            ("GET", "/oauth-providers"),
            ("GET", f"/tenants/{tenants['a']}/oauth-connections"),
            ("POST", f"/tenants/{tenants['a']}/oauth-connections/google/authorize"),
            ("POST", f"/tenants/{tenants['a']}/oauth-connections/callback"),
            ("DELETE", f"/tenants/{tenants['a']}/oauth-connections/{uuid.uuid4()}"),
        ):
            r = await c.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 401, (method, path)


# ── role gate and tenant scope ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["viewer_a", "supervisor_a"])
async def test_non_admin_roles_get_403_on_every_write_route(world, who):
    tenants, users = world
    headers = users[who]["headers"]
    async with _client() as c:
        base = f"/tenants/{tenants['a']}/oauth-connections"
        authorize = await c.post(f"{base}/google/authorize", headers=headers, json={})
        callback = await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"})
        disconnect = await c.delete(f"{base}/{uuid.uuid4()}", headers=headers)
    assert (authorize.status_code, callback.status_code, disconnect.status_code) == (403, 403, 403)


@pytest.mark.asyncio
async def test_an_admin_of_another_tenant_is_refused_on_every_route(world):
    tenants, users = world
    headers = users["admin_b"]["headers"]
    base = f"/tenants/{tenants['a']}/oauth-connections"
    async with _client() as c:
        responses = [
            await c.get(base, headers=headers),
            await c.post(f"{base}/google/authorize", headers=headers, json={}),
            await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"}),
            await c.delete(f"{base}/{uuid.uuid4()}", headers=headers),
        ]
    assert [r.status_code for r in responses] == [403, 403, 403, 403]


# ── request models ────────────────────────────────────────────────────────

def test_request_models_carry_no_tenant_or_redirect_field():
    assert set(OAuthAuthorizeRequest.model_fields) == {"preset_key"}
    assert set(OAuthCallbackRequest.model_fields) == {"state", "code", "accounts_server"}


@pytest.mark.asyncio
async def test_a_redirect_or_tenant_field_in_a_body_is_rejected(world):
    tenants, users = world
    headers = users["admin_a"]["headers"]
    base = f"/tenants/{tenants['a']}/oauth-connections"
    async with _client() as c:
        for extra in ({"redirect_uri": "https://evil.example"}, {"tenant_id": tenants["b"]}):
            assert (await c.post(f"{base}/google/authorize", headers=headers, json=extra)).status_code == 422
            r = await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c", **extra})
            assert r.status_code == 422


# ── the connect flow and its response shape ───────────────────────────────

@pytest.mark.asyncio
async def test_connect_list_and_disconnect_expose_only_the_public_key_set(world):
    tenants, users = world
    headers = users["admin_a"]["headers"]
    async with _client() as c:
        connected = await _connect(c, tenants["a"], headers)
        listed = await c.get(f"/tenants/{tenants['a']}/oauth-connections", headers=headers)
        disconnected = await c.delete(
            f"/tenants/{tenants['a']}/oauth-connections/{connected['id']}", headers=headers,
        )
        listed_after = await c.get(f"/tenants/{tenants['a']}/oauth-connections", headers=headers)

    assert set(connected) == CONNECTION_KEYS
    assert [set(row) for row in listed.json()] == [CONNECTION_KEYS]
    assert "provider_sub" not in listed.text and "enc:t1." not in listed.text
    assert disconnected.status_code == 200 and disconnected.json() == {"disconnected": True}
    assert listed_after.json()[0]["status"] == "disconnected"


@pytest.mark.asyncio
async def test_a_tenants_list_never_contains_another_tenants_connection(world):
    tenants, users = world
    async with _client() as c:
        await _connect(c, tenants["a"], users["admin_a"]["headers"])
        r = await c.get(f"/tenants/{tenants['b']}/oauth-connections", headers=users["admin_b"]["headers"])
    assert r.json() == []


@pytest.mark.asyncio
async def test_every_callback_failure_returns_the_same_400_body(world):
    tenants, users = world
    base = f"/tenants/{tenants['a']}/oauth-connections/callback"
    async with _client() as c:
        replayed = await _authorize(c, tenants["a"], users["admin_a"]["headers"])
        assert (await c.post(base, headers=users["admin_a"]["headers"], json={"state": replayed, "code": "c"})
                ).status_code == 200
        other_admins = await _authorize(c, tenants["a"], users["admin_a"]["headers"])
        bodies = [
            await c.post(base, headers=users["admin_a"]["headers"], json={"state": "forged", "code": "c"}),
            await c.post(base, headers=users["admin_a"]["headers"], json={"state": replayed, "code": "c"}),
            await c.post(base, headers=users["admin_a2"]["headers"], json={"state": other_admins, "code": "c"}),
        ]
    assert [r.status_code for r in bodies] == [400, 400, 400]
    assert {r.content for r in bodies} == {b'{"detail":"oauth_connection_failed"}'}


@pytest.mark.asyncio
async def test_disconnect_404_is_byte_identical_for_an_absent_and_a_foreign_id(world):
    """One principal (tenant B's admin) probes an id that does not exist and
    an id that is tenant A's connection (lesson 2: per-caller invariance)."""
    tenants, users = world
    async with _client() as c:
        a_connection = await _connect(c, tenants["a"], users["admin_a"]["headers"])
        base = f"/tenants/{tenants['b']}/oauth-connections"
        absent = await c.delete(f"{base}/{uuid.uuid4()}", headers=users["admin_b"]["headers"])
        foreign = await c.delete(f"{base}/{a_connection['id']}", headers=users["admin_b"]["headers"])
    assert absent.status_code == foreign.status_code == 404
    assert absent.content == foreign.content


# ── tripwires ─────────────────────────────────────────────────────────────

def test_every_tenant_scoped_handler_awaits_assert_tenant_access():
    """Lesson 38: an un-awaited async guard fails open. The expected set is
    derived from the router itself, so a new route cannot be added unseen."""
    tree = ast.parse(inspect.getsource(oauth_connections))
    handlers = {route.endpoint.__name__ for route in oauth_connections.tenant_scoped_router.routes}
    assert len(handlers) == 4

    awaited, bare = set(), []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "assert_tenant_access":
                bare.append(node)
            if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "assert_tenant_access"):
                awaited.add(fn.name)
    assert len(bare) == len(handlers)           # every call site found...
    assert awaited == handlers                  # ...and each one is awaited, in every handler
