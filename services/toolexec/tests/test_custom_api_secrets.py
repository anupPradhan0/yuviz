"""
Credentials on the custom-API admin surface: sealed to the tenant on write,
masked on every read, and never accepted back as a pasted ref.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import decrypt_tenant_secret, encrypt_secret
from services.config import auth
from services.toolexec import custom_apis
from services.toolexec.app import app
from services.toolexec.routers import custom_apis as custom_apis_router
from services.toolexec.tests.test_chain_execution import _fake_dns  # noqa: F401  (autouse fixture)


def _api(tenant, **overrides) -> dict:
    kwargs = dict(
        tenant_id=str(tenant["id"]), name=f"s_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/api", method="GET",
    )
    kwargs.update(overrides)
    return kwargs


async def _stored_auth_config(pool, api_id) -> dict:
    row = await pool.fetchrow("SELECT auth_config::text AS c FROM custom_apis WHERE id = $1", api_id)
    return json.loads(row["c"])


async def _headers(pool, tenant_id, role: str) -> dict:
    user = await pool.fetchrow(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', $3) RETURNING id",
        tenant_id, f"{uuid.uuid4().hex[:8]}@t10.example", role,
    )
    token = auth.create_access_token({
        "id": str(user["id"]), "email": "x@t10.example", "role": role,
        "tenant_id": str(tenant_id), "is_service_account": False,
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def users_cleanup(pool):
    yield
    users = "SELECT id FROM users WHERE email LIKE '%@t10.example'"
    await pool.execute(f"DELETE FROM audit_log WHERE user_id IN ({users})")
    await pool.execute("DELETE FROM users WHERE email LIKE '%@t10.example'")


@pytest.mark.asyncio
async def test_auth_secrets_are_sealed_to_the_tenant(pool, tenant_agent):
    tenant, _ = tenant_agent
    api = await custom_apis.create_custom_api(**_api(
        tenant, auth_scheme="bearer", auth_config={}, auth_secrets={"token_ref": "sk-plain"},
    ))
    ref = (await _stored_auth_config(pool, api["id"]))["token_ref"]
    assert ref.startswith("enc:t1.")
    assert decrypt_tenant_secret(tenant["id"], ref) == "sk-plain"
    with pytest.raises(ValueError):
        decrypt_tenant_secret(uuid.uuid4(), ref)


@pytest.mark.asyncio
async def test_stored_sentinel_keeps_the_stored_ref_byte_identical(pool, tenant_agent):
    tenant, _ = tenant_agent
    api = await custom_apis.create_custom_api(**_api(
        tenant, auth_scheme="bearer", auth_secrets={"token_ref": "sk-plain"},
    ))
    before = (await _stored_auth_config(pool, api["id"]))["token_ref"]
    await custom_apis.update_custom_api(api["id"], auth_config={"token_ref": "[stored]"}, description="edited")
    assert (await _stored_auth_config(pool, api["id"]))["token_ref"] == before
    # An unrelated edit that does not mention auth at all keeps it too.
    await custom_apis.update_custom_api(api["id"], description="edited again")
    assert (await _stored_auth_config(pool, api["id"]))["token_ref"] == before


@pytest.mark.asyncio
async def test_stored_sentinel_is_refused_on_create_and_on_scheme_change(pool, tenant_agent):
    tenant, _ = tenant_agent
    with pytest.raises(ValueError, match="credential_ref_not_a_reference"):
        await custom_apis.create_custom_api(**_api(
            tenant, auth_scheme="bearer", auth_config={"token_ref": "[stored]"},
        ))
    api = await custom_apis.create_custom_api(**_api(
        tenant, auth_scheme="bearer", auth_secrets={"token_ref": "sk-plain"},
    ))
    with pytest.raises(ValueError, match="credential_ref_not_a_reference"):
        await custom_apis.update_custom_api(
            api["id"], auth_scheme="api_key", auth_config={"key_ref": "[stored]", "name": "X"},
        )


@pytest.mark.asyncio
async def test_a_value_and_a_secret_for_one_field_is_ambiguous(tenant_agent):
    tenant, _ = tenant_agent
    with pytest.raises(ValueError, match="credential_ref_ambiguous"):
        await custom_apis.create_custom_api(**_api(
            tenant, auth_scheme="bearer", auth_config={"token_ref": "env:X"}, auth_secrets={"token_ref": "k"},
        ))


@pytest.mark.asyncio
async def test_an_auth_secrets_key_outside_the_scheme_is_refused(tenant_agent):
    tenant, _ = tenant_agent
    with pytest.raises(ValueError, match="credential_ref_not_a_reference"):
        await custom_apis.create_custom_api(**_api(
            tenant, auth_scheme="bearer", auth_secrets={"client_secret_ref": "k"},
        ))


@pytest.mark.asyncio
async def test_a_viewer_never_reads_a_ref_of_any_scheme(pool, tenant_agent, users_cleanup):
    tenant, _ = tenant_agent
    sealed = await custom_apis.create_custom_api(**_api(
        tenant, auth_scheme="bearer", auth_secrets={"token_ref": "sk-plain"},
    ))
    pointer = await custom_apis.create_custom_api(**_api(tenant, auth_scheme="bearer", auth_secrets={"token_ref": "x"}))
    quarantined = await custom_apis.create_custom_api(**_api(tenant, auth_scheme="bearer", auth_secrets={"token_ref": "x"}))
    for api_id, value in ((pointer["id"], "env:PLATFORM_JWT_SECRET"), (quarantined["id"], custom_apis.QUARANTINED)):
        await pool.execute(
            "UPDATE custom_apis SET auth_config = $2::jsonb WHERE id = $1", api_id, json.dumps({"token_ref": value}),
        )
    headers = await _headers(pool, tenant["id"], "viewer")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        listed = (await c.get(f"/tenants/{tenant['id']}/custom-apis", headers=headers)).json()
        by_id = {row["id"]: row for row in listed}
        singles = {
            str(a["id"]): (await c.get(f"/custom-apis/{a['id']}", headers=headers)).json()
            for a in (sealed, pointer, quarantined)
        }

    for body in (listed, singles):
        text = json.dumps(body)
        for prefix in ("enc:", "env:", "k8s:"):
            assert f'"{prefix}' not in text
    assert by_id[str(sealed["id"])]["auth_config"]["token_ref"] == "[stored]"
    assert by_id[str(pointer["id"])]["auth_config"]["token_ref"] == "[stored]"
    assert by_id[str(quarantined["id"])]["auth_config"]["token_ref"] == ""
    assert singles[str(sealed["id"])]["auth_config"]["token_ref"] == "[stored]"


@pytest.mark.asyncio
async def test_create_and_update_responses_are_masked(pool, tenant_agent, users_cleanup):
    tenant, _ = tenant_agent
    headers = await _headers(pool, tenant["id"], "admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        created = await c.post(f"/tenants/{tenant['id']}/custom-apis", headers=headers, json={
            "name": f"r_{uuid.uuid4().hex[:8]}", "description": "d", "endpoint_url": "https://example.com/a",
            "method": "GET", "auth_scheme": "bearer", "auth_secrets": {"token_ref": "sk-plain"},
        })
        assert created.status_code == 201
        assert created.json()["auth_config"]["token_ref"] == "[stored]"
        api_id = created.json()["id"]
        updated = await c.patch(f"/custom-apis/{api_id}", headers=headers, json={
            "auth_config": {"token_ref": "[stored]"}, "description": "x",
        })
        assert updated.status_code == 200
        assert updated.json()["auth_config"]["token_ref"] == "[stored]"
        assert "enc:" not in created.text + updated.text


@pytest.mark.asyncio
async def test_pasting_another_tenants_real_ref_is_refused(pool, tenant_agent, users_cleanup):
    tenant_b, _ = tenant_agent
    tenant_a = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ('A', $1) RETURNING *", f"t10-a-{uuid.uuid4().hex[:8]}",
    ))
    try:
        a_api = await custom_apis.create_custom_api(**_api(
            tenant_a, auth_scheme="bearer", auth_secrets={"token_ref": "a-secret"},
        ))
        a_ref = (await _stored_auth_config(pool, a_api["id"]))["token_ref"]
        headers = await _headers(pool, tenant_b["id"], "admin")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for ref in (a_ref, encrypt_secret("legacy")):
                r = await c.post(f"/tenants/{tenant_b['id']}/custom-apis", headers=headers, json={
                    "name": f"p_{uuid.uuid4().hex[:8]}", "description": "d",
                    "endpoint_url": "https://example.com/a", "method": "GET",
                    "auth_scheme": "bearer", "auth_config": {"token_ref": ref},
                })
                assert r.status_code == 400
                assert r.json()["detail"] == "credential_ref_outside_tenant_namespace: token_ref"
    finally:
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant_a["id"])
        await pool.execute("DELETE FROM audit_log WHERE tenant_id = $1", tenant_a["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", tenant_a["id"])


def test_every_dict_returning_custom_api_route_masks():
    """Derived from every APIRouter in the router module (lesson 29): a new
    route that returns a row and skips public_custom_api fails here."""
    routers = [v for v in vars(custom_apis_router).values() if isinstance(v, APIRouter)]
    routes = [
        r for router in routers for r in router.routes
        if isinstance(r, APIRoute) and r.status_code != 204
    ]
    assert len(routes) == 4  # list, create, get, patch
    for route in routes:
        assert "public_custom_api(" in inspect.getsource(route.endpoint), route.path
