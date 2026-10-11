"""The sentiment backfill must never score calls of agents that opted out."""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg

os.environ.setdefault("POSTGRES_DSN", "postgresql://localhost:5432/voiceai")

_spec = importlib.util.spec_from_file_location(
    "backfill_call_sentiment", Path(__file__).resolve().parents[1] / "scripts/backfill_call_sentiment.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


async def test_only_opted_in_agents_calls_are_selected():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    tag = uuid.uuid4().hex[:8]
    tenant = await conn.fetchrow(
        "INSERT INTO tenants (slug, name) VALUES ($1, 'Backfill test') RETURNING id, slug", f"bf-{tag}")
    try:
        on = await conn.fetchval(
            "INSERT INTO agents (tenant_id, slug, name, sentiment_analysis_enabled) "
            "VALUES ($1, 'on', 'On', true) RETURNING id", tenant["id"])
        off = await conn.fetchval(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'off', 'Off') RETURNING id", tenant["id"])
        for agent_id, sid in ((on, f"bf-on-{tag}"), (off, f"bf-off-{tag}"), (None, f"bf-none-{tag}")):
            await conn.execute(
                "INSERT INTO calls (session_id, tenant_id, agent_id, direction, ended_at) "
                "VALUES ($1, $2, $3, 'inbound', now())", sid, tenant["slug"], agent_id)

        sql, params = backfill.unscored_calls_query(tenant["slug"], None)

        assert [r["session_id"] for r in await conn.fetch(sql, *params)] == [f"bf-on-{tag}"]
    finally:
        await conn.execute("DELETE FROM calls WHERE tenant_id = $1", tenant["slug"])
        await conn.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
        await conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
        await conn.close()
