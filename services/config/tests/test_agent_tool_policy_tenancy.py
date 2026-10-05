"""A tool policy can only reference a provider config of the agent's own tenant.

Two real tenants: `test_tenant` (the caller's, with `test_admin`) and a second one that owns the
foreign provider config.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config.app import app

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def client(test_admin):
    headers = {"Authorization": f"Bearer {test_admin['token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def own(pool, test_tenant):
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", test_tenant["id"],
    ))
    config = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'Own', 'execute_api', 'toolexec') RETURNING *", test_tenant["id"],
    ))
    return {"agent": agent, "config": config}


@pytest_asyncio.fixture(loop_scope="session")
async def foreign_config(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    config = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine, api_key_ref) "
        "VALUES ($1, 'Theirs', 'execute_api', 'toolexec', 'env:THEIR_SECRET') RETURNING *", tenant["id"],
    ))
    yield config
    await pool.execute("DELETE FROM tool_provider_configs WHERE id = $1", config["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def _policies(pool, agent_id) -> list:
    return await pool.fetch("SELECT * FROM agent_tool_policies WHERE agent_id = $1", agent_id)


def _create(client, agent_id, config_id):
    return client.post(
        f"/agents/{agent_id}/tool-policies", json={"tool_name": "execute_api", "tool_provider_config_id": config_id},
    )


async def test_own_config_is_accepted(client, pool, own):
    resp = await _create(client, own["agent"]["id"], str(own["config"]["id"]))
    assert resp.status_code == 201
    assert len(await _policies(pool, own["agent"]["id"])) == 1


async def test_foreign_config_is_refused_like_a_nonexistent_one(client, pool, own, foreign_config):
    foreign_id = str(foreign_config["id"])
    foreign_resp = await _create(client, own["agent"]["id"], foreign_id)
    unknown_resp = await _create(client, own["agent"]["id"], UNKNOWN_ID)

    assert unknown_resp.status_code == 404
    assert foreign_resp.status_code == unknown_resp.status_code
    assert foreign_resp.json()["detail"] == unknown_resp.json()["detail"].replace(UNKNOWN_ID, foreign_id)
    assert await _policies(pool, own["agent"]["id"]) == []


async def test_soft_deleted_config_is_refused(client, pool, own):
    await pool.execute("UPDATE tool_provider_configs SET deleted_at = now() WHERE id = $1", own["config"]["id"])
    assert (await _create(client, own["agent"]["id"], str(own["config"]["id"]))).status_code == 404


async def test_malformed_config_id_is_the_same_404(client, own):
    assert (await _create(client, own["agent"]["id"], "not-a-uuid")).status_code == 404


async def test_database_refuses_a_cross_tenant_policy(pool, own, foreign_config):
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id) "
            "VALUES ($1, 'execute_api', $2)", own["agent"]["id"], foreign_config["id"],
        )


async def test_listing_hides_a_stray_cross_tenant_policy(client, pool, own, foreign_config):
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("ALTER TABLE agent_tool_policies DISABLE TRIGGER agent_tool_policies_same_tenant")
        await conn.execute(
            "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id) "
            "VALUES ($1, 'execute_api', $2)", own["agent"]["id"], foreign_config["id"],
        )
        await conn.execute("ALTER TABLE agent_tool_policies ENABLE TRIGGER agent_tool_policies_same_tenant")

    resp = await client.get(f"/agents/{own['agent']['id']}/tool-policies")
    assert resp.status_code == 200
    assert resp.json() == []
    await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", own["agent"]["id"])
