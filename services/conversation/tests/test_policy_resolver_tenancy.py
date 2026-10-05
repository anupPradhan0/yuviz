"""ToolPolicyResolver never resolves another tenant's provider config (real Postgres).

A cross-tenant policy row can only exist if it predates the same-tenant trigger, so the trigger is
disabled just long enough to plant one.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest_asyncio

from services.conversation.tools.policy_resolver import ToolPolicyResolver
from services.conversation.tools.registry import SEARCH_KNOWLEDGE, ToolRegistry

POSTGRES_DSN = os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=2)
    yield p
    await p.close()


async def _tenant(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))
    config = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine, api_key_ref) "
        "VALUES ($1, 'Custom APIs', 'search_knowledge', 'toolexec', 'env:TENANT_SECRET') RETURNING *", tenant["id"],
    ))
    return {"tenant": tenant, "agent": agent, "config": config}


async def _drop(pool, t):
    await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", t["agent"]["id"])
    await pool.execute("DELETE FROM agents WHERE id = $1", t["agent"]["id"])
    await pool.execute("DELETE FROM tool_provider_configs WHERE id = $1", t["config"]["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", t["tenant"]["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def two_tenants(pool):
    a, b = await _tenant(pool), await _tenant(pool)
    yield a, b
    await _drop(pool, a)
    await _drop(pool, b)


async def _plant_policy(pool, agent_id, config_id):
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("ALTER TABLE agent_tool_policies DISABLE TRIGGER agent_tool_policies_same_tenant")
        await conn.execute(
            "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id) "
            "VALUES ($1, 'search_knowledge', $2)", agent_id, config_id,
        )
        await conn.execute("ALTER TABLE agent_tool_policies ENABLE TRIGGER agent_tool_policies_same_tenant")


async def _resolve(pool, t):
    registry = ToolRegistry()
    registry.register(SEARCH_KNOWLEDGE)
    resolver = ToolPolicyResolver(pool, registry, cache_ttl_s=0)
    return await resolver.enabled_tools(str(t["agent"]["id"]), t["tenant"]["slug"])


async def test_own_tenant_config_resolves(pool, two_tenants):
    a, _ = two_tenants
    await _plant_policy(pool, a["agent"]["id"], a["config"]["id"])

    resolved = await _resolve(pool, a)

    assert [p.tool_provider_config_id for p in resolved] == [str(a["config"]["id"])]
    assert resolved[0].api_key_ref == "env:TENANT_SECRET"


async def test_another_tenants_config_never_resolves(pool, two_tenants):
    a, b = two_tenants
    await _plant_policy(pool, a["agent"]["id"], b["config"]["id"])

    assert await _resolve(pool, a) == []
