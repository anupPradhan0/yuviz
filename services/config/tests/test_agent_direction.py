"""Agent call direction survives create, update, and list responses."""

import pytest

from services.config import agents


@pytest.mark.asyncio
async def test_direction_roundtrip_and_default(test_tenant, scoped):
    outbound = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="outbound-direction", name="Outbound",
        direction="outbound", status="inactive",
    )
    assert outbound["direction"] == "outbound"
    changed = await agents.update_agent(
        outbound["id"], tenant_slug=test_tenant["slug"], direction="both",
    )
    assert changed["direction"] == "both"
    listed = await agents.list_agents(test_tenant["id"])
    assert next(agent for agent in listed if agent["id"] == outbound["id"])["direction"] == "both"

    inbound = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="default-direction", name="Inbound",
    )
    assert inbound["direction"] == "inbound"
