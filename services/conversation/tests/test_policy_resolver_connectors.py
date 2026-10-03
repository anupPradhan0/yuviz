"""
ToolPolicyResolver against a real Postgres, for what the canned-row tests in
test_execute_api_tool.py cannot show because they never run the query:

  - a `caller_id` param (the call's own remote party, filled by toolexec)
    never reaches the schema the model sees;
  - an API behind an OAuth connector disappears from the model's tool list
    while the connection is not `connected`, and another tenant's connected
    connection does not bring it back.

Needs the schema applied to POSTGRES_DSN, like test_transcript_builder.py.
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from services.conversation.tools.policy_resolver import ToolPolicyResolver
from services.conversation.tools.registry import ToolRegistry

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")


@pytest_asyncio.fixture
async def world():
    pool = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], min_size=1, max_size=2)
    slug = f"connectors-{uuid.uuid4().hex[:8]}"
    tenant = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING id", slug)
    agent = await pool.fetchval(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'a', 'A') RETURNING id", tenant)
    config = await pool.fetchval(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'c', 'execute_api', 'toolexec') RETURNING id", tenant)
    await pool.execute(
        "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id) VALUES ($1, 'execute_api', $2)",
        agent, config)
    state = {"pool": pool, "slug": slug, "tenant": tenant, "agent": agent, "tenants": [tenant]}
    yield state
    ids = state["tenants"]
    await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent)
    await pool.execute("DELETE FROM custom_api_params WHERE custom_api_id IN "
                       "(SELECT id FROM custom_apis WHERE tenant_id = ANY($1::uuid[]))", ids)
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", agent)
    await pool.execute("DELETE FROM tool_provider_configs WHERE tenant_id = $1", tenant)
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)
    await pool.close()


async def _connection(world, status: str, tenant=None) -> uuid.UUID:
    tenant = tenant or world["tenant"]
    if status == "connected":
        return await world["pool"].fetchval(
            "INSERT INTO oauth_connections (tenant_id, provider, status, access_token_ref, refresh_token_ref, "
            "access_expires_at) VALUES ($1, 'google', 'connected', 'enc:t1.x', 'enc:t1.y', now() + interval '1 hour') "
            "RETURNING id", tenant)
    return await world["pool"].fetchval(
        "INSERT INTO oauth_connections (tenant_id, provider, status) VALUES ($1, 'google', $2) RETURNING id",
        tenant, status)


async def _api(world, name: str, params=(), connection=None) -> uuid.UUID:
    pool = world["pool"]
    api = await pool.fetchval(
        "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method, auth_scheme, oauth_connection_id) "
        "VALUES ($1, $2, 'd', 'https://www.googleapis.com/x', 'GET', $3, $4) RETURNING id",
        world["tenant"], name, "oauth2_authorization_code" if connection else "none", connection)
    for param_name, source in params:
        await pool.execute(
            "INSERT INTO custom_api_params (custom_api_id, name, location, json_type, source, literal_value) "
            "VALUES ($1, $2, 'query', 'string', $3, $4::jsonb)",
            api, param_name, source, json.dumps("v") if source == "literal" else None)
    await pool.execute("INSERT INTO agent_custom_apis (agent_id, custom_api_id) VALUES ($1, $2)", world["agent"], api)
    return api


async def _offered(world) -> list[str]:
    resolver = ToolPolicyResolver(pool=world["pool"], registry=ToolRegistry(), cache_ttl_s=0)
    policies = await resolver.enabled_tools(str(world["agent"]), world["slug"])
    if not policies:
        return []
    return policies[0].definition.parameters_schema["properties"]["api_name"]["enum"]


async def _specialized(world):
    resolver = ToolPolicyResolver(pool=world["pool"], registry=ToolRegistry(), cache_ttl_s=0)
    return (await resolver.enabled_tools(str(world["agent"]), world["slug"]))[0]


@pytest.mark.asyncio
async def test_a_caller_id_param_is_never_in_the_schema_the_model_sees(world):
    await _api(world, "book", params=[("patient_name", "caller"), ("caller_phone", "caller_id"),
                                       ("yuviz_preset", "literal")])

    policy = await _specialized(world)

    shown = json.dumps(policy.definition.parameters_schema) + policy.definition.description
    assert "* patient_name (string, required)" in policy.definition.description  # the query did run and list callers
    assert "caller_phone" not in shown
    assert "yuviz_preset" not in shown
    assert "caller_phone" not in policy.sensitive_arg_keys


@pytest.mark.asyncio
async def test_an_api_behind_a_connected_connector_is_offered(world):
    await _api(world, "plain")
    await _api(world, "calendar", connection=await _connection(world, "connected"))

    assert await _offered(world) == ["calendar", "plain"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disconnected", "reconnect_needed"])
async def test_an_api_behind_a_connector_that_is_not_connected_is_not_offered(world, status):
    await _api(world, "plain")
    await _api(world, "calendar", connection=await _connection(world, status))

    assert await _offered(world) == ["plain"]


@pytest.mark.asyncio
async def test_another_tenants_connected_connection_does_not_bring_it_back(world):
    await _api(world, "calendar", connection=await _connection(world, "disconnected"))
    other = await world["pool"].fetchval(
        "INSERT INTO tenants (name, slug) VALUES ('O', $1) RETURNING id", f"o-{uuid.uuid4().hex[:8]}")
    world["tenants"].append(other)
    await _connection(world, "connected", tenant=other)

    assert await _offered(world) == []


@pytest.mark.asyncio
async def test_reconnecting_the_same_row_offers_the_api_again_with_no_repointing(world):
    connection = await _connection(world, "disconnected")
    await _api(world, "calendar", connection=connection)
    assert await _offered(world) == []

    await world["pool"].execute(
        "UPDATE oauth_connections SET status = 'connected', access_token_ref = 'enc:t1.x', "
        "refresh_token_ref = 'enc:t1.y', access_expires_at = now() + interval '1 hour' WHERE id = $1", connection)

    assert await _offered(world) == ["calendar"]


@pytest.mark.asyncio
async def test_a_soft_deleted_connection_does_not_offer_its_apis(world):
    connection = await _connection(world, "connected")
    await _api(world, "calendar", connection=connection)
    assert await _offered(world) == ["calendar"]

    await world["pool"].execute("UPDATE oauth_connections SET deleted_at = now() WHERE id = $1", connection)

    assert await _offered(world) == []
