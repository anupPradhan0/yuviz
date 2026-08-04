from __future__ import annotations

import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("KNOWLEDGE_STORAGE_ROOT", "/tmp/voiceai-knowledge-test-storage")

import pytest_asyncio

from services.knowledge import db  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_agent(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Test {slug}", slug,
    ))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))
    yield tenant, agent
    await pool.execute("DELETE FROM agent_retrieval_policies WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
    await pool.execute(
        "DELETE FROM kb_ingestion_jobs WHERE document_id IN "
        "(SELECT id FROM kb_documents WHERE tenant_id = $1)", tenant["id"],
    )
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
