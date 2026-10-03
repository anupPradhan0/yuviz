"""
Toolexec refuses legacy and pointer refs at write time and refuses legacy
refs at call time. A fallback to an unbound Fernet decrypt would defeat the
tenant binding, so every legacy case here asserts a refusal AND that the
mock upstream saw zero requests.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from libs.config_sdk.secrets import encrypt_secret, encrypt_tenant_secret
from services.config.auth import CurrentUser
from services.toolexec import agent_apis, auth_schemes, custom_apis, executor
from services.toolexec.schemas import ChainExecuteRequest

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


def test_validate_accepts_own_tenant_bound_ref():
    auth_schemes.validate_tenant_ref(TENANT_A, encrypt_tenant_secret(TENANT_A, "k"))


def test_validate_rejects_another_tenants_bound_ref():
    ref = encrypt_tenant_secret(TENANT_A, "k")
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        auth_schemes.validate_tenant_ref(TENANT_B, ref)


@pytest.mark.parametrize("ref", [
    encrypt_secret("legacy-key"),
    f"env:TENANT_{uuid.UUID(TENANT_A).hex.upper()}_TOKEN",
    f"k8s:tenants/{TENANT_A}/token",
    "sk-a-literal",
])
def test_validate_rejects_legacy_pointer_and_literal(ref):
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        auth_schemes.validate_tenant_ref(TENANT_A, ref)


@pytest.mark.asyncio
async def test_resolve_refuses_legacy_enc_without_touching_the_resolver(monkeypatch):
    async def _fail(ref):
        raise AssertionError("legacy enc: reached the shared resolver")

    monkeypatch.setattr(auth_schemes._tenant_secret_resolver, "resolve", _fail)
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        await auth_schemes.resolve_tenant_ref(TENANT_A, encrypt_secret("legacy-key"))


@pytest.mark.asyncio
async def test_resolve_refuses_another_tenants_bound_ref():
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        await auth_schemes.resolve_tenant_ref(TENANT_B, encrypt_tenant_secret(TENANT_A, "k"))


@pytest.mark.asyncio
async def test_resolve_still_reads_a_preexisting_env_row():
    os.environ[f"TENANT_{uuid.UUID(TENANT_A).hex.upper()}_LEGACY"] = "env-value"
    value = await auth_schemes.resolve_tenant_ref(TENANT_A, f"env:TENANT_{uuid.UUID(TENANT_A).hex.upper()}_LEGACY")
    assert value == "env-value"


# ── end to end: a row that already holds a bad ref executes to credential_unavailable ──

_ENC_BOUND = "custom_apis_auth_config_enc_bound"


async def _bearer_api(pool, tenant, agent, auth_config: dict) -> dict:
    name = f"t9_{uuid.uuid4().hex[:8]}"
    api = await custom_apis.create_custom_api(
        tenant_id=str(tenant["id"]), name=name, description="d",
        endpoint_url=f"https://{name}.example.com/api", method="GET", side_effecting=False,
        auth_scheme="bearer", auth_config={"token_ref": encrypt_tenant_secret(tenant["id"], "seed")},
    )
    # Direct write: the state a row is in when it predates the write-side
    # refusal. custom_apis_auth_config_enc_bound is NOT VALID, so it tolerates
    # rows that already hold a legacy enc: ref but still rejects a new write of
    # one — including this seed. Dropping it for the write is what lets the
    # test reach the row state it is about; re-added NOT VALID, exactly as
    # schema.sql declares it, so nothing else in the session sees a difference.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(f"ALTER TABLE custom_apis DROP CONSTRAINT {_ENC_BOUND}")
        await conn.execute(
            "UPDATE custom_apis SET auth_config = $2::jsonb WHERE id = $1", api["id"], json.dumps(auth_config),
        )
        await conn.execute(
            f"ALTER TABLE custom_apis ADD CONSTRAINT {_ENC_BOUND} "
            "CHECK (auth_config::text !~ '\"enc:(?!t1\\.)') NOT VALID"
        )
    admin = CurrentUser(id=str(uuid.uuid4()), email="a@test.example", role="admin", tenant_id=str(tenant["id"]))
    await agent_apis.set_enabled(agent["id"], api["id"], enabled=True, current_user=admin)
    return api


async def _run(tenant, agent, api, monkeypatch) -> tuple[object, list]:
    calls: list = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", lambda ips: httpx.MockTransport(handler))
    unique = uuid.uuid4().hex[:8]
    response = await executor.execute_chain(ChainExecuteRequest(
        tenant_id=str(tenant["id"]), agent_id=str(agent["id"]), call_id=f"c-{unique}",
        session_id=f"s-{unique}", turn_id=f"t-{unique}", tool_call_id=f"tc-{unique}",
        idempotency_key=f"i-{unique}", api_name=api["name"], caller_arguments={},
        chain_budget_ms=15000, max_chain_depth=4,
    ))
    return response, calls


@pytest.mark.asyncio
async def test_a_legacy_fernet_row_fails_closed_with_zero_requests(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _bearer_api(pool, tenant, agent, {"token_ref": encrypt_secret("legacy-key")})
    response, calls = await _run(tenant, agent, api, monkeypatch)
    assert response.chain_status == "unavailable"
    assert calls == []
    step = await pool.fetchrow("SELECT error FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id))
    assert step["error"] == "credential_unavailable"


@pytest.mark.asyncio
async def test_another_tenants_bound_ref_in_this_row_fails_closed(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _bearer_api(pool, tenant, agent, {"token_ref": encrypt_tenant_secret(TENANT_A, "a-key")})
    response, calls = await _run(tenant, agent, api, monkeypatch)
    assert response.chain_status == "unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_a_direct_env_row_still_resolves_until_quarantined(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    hex_ = uuid.UUID(str(tenant["id"])).hex.upper()
    monkeypatch.setenv(f"TENANT_{hex_}_LIVE", "env-token")
    api = await _bearer_api(pool, tenant, agent, {"token_ref": f"env:TENANT_{hex_}_LIVE"})
    response, calls = await _run(tenant, agent, api, monkeypatch)
    assert response.chain_status == "success"
    assert calls[0].headers["Authorization"] == "Bearer env-token"


@pytest.mark.asyncio
async def test_registering_a_foreign_bound_or_legacy_or_env_ref_is_refused(tenant_agent):
    tenant, _ = tenant_agent
    hex_ = uuid.UUID(str(tenant["id"])).hex.upper()
    for ref in (encrypt_tenant_secret(TENANT_A, "k"), encrypt_secret("k"), f"env:TENANT_{hex_}_X"):
        with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
            await custom_apis.create_custom_api(
                tenant_id=str(tenant["id"]), name=f"t9_{uuid.uuid4().hex[:8]}", description="d",
                endpoint_url="https://x.example.com/a", method="GET",
                auth_scheme="bearer", auth_config={"token_ref": ref},
            )
