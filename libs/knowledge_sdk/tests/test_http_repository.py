"""HttpKnowledgeRepository against the real Config (auth) and Knowledge apps in-process."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from httpx import ASGITransport

from libs.knowledge_sdk.exceptions import RepositoryUnavailableError
from libs.knowledge_sdk.models import RetrievalPolicy
from libs.knowledge_sdk.repositories.http_repository import HttpKnowledgeRepository
from libs.tenancy import set_target_tenant
from services.config import agents, provider_configs, tenants, users
from services.config import db as config_db
from services.config.app import app as config_app
from services.knowledge.app import app as knowledge_app


@contextmanager
def _seeding_as(tenant_id):
    """Binds the tenant only while seeding via Config's service functions,
    so the HTTP calls under test resolve their own scope."""
    set_target_tenant(str(tenant_id))
    try:
        yield
    finally:
        set_target_tenant(None)


@pytest.fixture
async def service_account():
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="service-password", role="viewer")
    # Same as scripts/create_service_account.py.
    pool = await config_db.get_pool()
    await pool.execute("UPDATE users SET is_service_account = true WHERE id = $1", user["id"])
    yield {"email": email, "password": "service-password"}
    from services.config import db
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


def _repo(service_account) -> HttpKnowledgeRepository:
    return HttpKnowledgeRepository(
        base_url="http://knowledge-test",
        auth_base_url="http://config-test",
        service_email=service_account["email"],
        service_password=service_account["password"],
        transport=ASGITransport(app=knowledge_app),
        auth_transport=ASGITransport(app=config_app),
    )


@pytest.fixture
async def tenant_agent(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = await tenants.create_tenant(name="Test Tenant", slug=slug)
    with _seeding_as(tenant["id"]):
        agent = await agents.create_agent(tenant_id=tenant["id"], slug="sup", name="Support", tenant_slug=slug)
    yield tenant, agent
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_has_enabled_kb_false_when_nothing_attached(service_account, tenant_agent):
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    assert await repo.has_enabled_kb(tenant["slug"], agent["slug"]) is False
    await repo.close()


async def test_has_enabled_kb_true_after_assignment(service_account, tenant_agent, pool):
    tenant, agent = tenant_agent
    with _seeding_as(tenant["id"]):
        embedding_cfg = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
            allow_pointer_schemes=False,
        )
    kb = await pool.fetchrow(
        "INSERT INTO knowledge_bases (tenant_id, slug, name, embedding_config_id) "
        "VALUES ($1, 'policies', 'Policies', $2) RETURNING *",
        tenant["id"], embedding_cfg["id"],
    )
    await pool.execute(
        "INSERT INTO agent_knowledge_bases (agent_id, kb_id, enabled) VALUES ($1, $2, true)",
        agent["id"], kb["id"],
    )

    repo = _repo(service_account)
    assert await repo.has_enabled_kb(tenant["slug"], agent["slug"]) is True
    await repo.close()


async def test_retrieve_with_nothing_relevant_is_200_no_match_not_404(service_account, tenant_agent):
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "anything", RetrievalPolicy())
    assert result["status"] == "no_match"
    assert result["chunks"] == [] and result["trace_id"]
    await repo.close()


def _stub_retrieve(monkeypatch, impl):
    from services.knowledge.routers import retrieve as router_module
    monkeypatch.setattr(router_module.retrieval_service, "retrieve", impl)


async def test_router_passes_through_a_result_that_already_has_a_status(service_account, tenant_agent, monkeypatch):
    async def partial(conn, *a, **kw):
        return {"status": "partial", "chunks": [{"content": "x"}], "degraded": ["rerank_timeout"], "trace_id": "abc"}
    _stub_retrieve(monkeypatch, partial)
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "q", RetrievalPolicy())
    assert (result["status"], result["degraded"], result["trace_id"]) == ("partial", ["rerank_timeout"], "abc")
    await repo.close()


async def test_router_adds_status_ok_to_a_result_from_before_the_status_contract(service_account, tenant_agent, monkeypatch):
    async def old_shape(conn, *a, **kw):
        return {"chunks": [{"content": "x"}], "sources": [], "token_count": 1}
    _stub_retrieve(monkeypatch, old_shape)
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "q", RetrievalPolicy())
    assert result["status"] == "ok" and result["trace_id"]
    await repo.close()


async def test_router_turns_a_none_result_into_no_match(service_account, tenant_agent, monkeypatch):
    async def nothing(conn, *a, **kw):
        return None
    _stub_retrieve(monkeypatch, nothing)
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "q", RetrievalPolicy())
    assert result["status"] == "no_match" and result["chunks"] == []
    await repo.close()


async def test_router_turns_an_exception_into_unavailable_without_leaking_its_text(
    service_account, tenant_agent, monkeypatch,
):
    async def boom(conn, *a, **kw):
        raise RuntimeError("connect failed postgres://svc:hunter2@db.internal/knowledge")
    _stub_retrieve(monkeypatch, boom)
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "q", RetrievalPolicy())
    assert result["status"] == "unavailable" and result["chunks"] == []
    assert result["degraded"] == ["retrieval_error"]
    assert "hunter2" not in str(result)
    await repo.close()


async def test_router_turns_a_slow_retrieval_into_unavailable_timeout(service_account, tenant_agent, monkeypatch):
    import asyncio

    async def slow(conn, *a, **kw):
        await asyncio.sleep(5)
    _stub_retrieve(monkeypatch, slow)
    monkeypatch.setenv("KNOWLEDGE_RETRIEVE_TIMEOUT_S", "0.05")
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "q", RetrievalPolicy())
    assert result["status"] == "unavailable" and result["degraded"] == ["timeout"]
    await repo.close()


async def test_client_still_reads_the_old_404_as_no_context_and_an_old_200_without_status_as_a_dict():
    import httpx

    def _repo_answering(response: httpx.Response) -> HttpKnowledgeRepository:
        return HttpKnowledgeRepository(
            base_url="http://k", auth_base_url="http://c", service_email="e", service_password="p",
            transport=httpx.MockTransport(lambda request: response),
            auth_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "t"})),
        )

    old_404 = _repo_answering(httpx.Response(404, json={"detail": "no eligible context"}))
    assert await old_404.retrieve("acme", "sup", "q", RetrievalPolicy()) is None
    await old_404.close()

    old_200 = _repo_answering(httpx.Response(200, json={"chunks": [{"content": "x"}], "sources": [], "token_count": 1}))
    result = await old_200.retrieve("acme", "sup", "q", RetrievalPolicy())
    assert result == {"chunks": [{"content": "x"}], "sources": [], "token_count": 1} and "status" not in result
    await old_200.close()


async def test_wrong_service_password_raises_unavailable(pool):
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="correct-password", role="viewer")

    repo = HttpKnowledgeRepository(
        base_url="http://knowledge-test", auth_base_url="http://config-test",
        service_email=email, service_password="wrong-password",
        transport=ASGITransport(app=knowledge_app), auth_transport=ASGITransport(app=config_app),
    )
    with pytest.raises(RepositoryUnavailableError):
        await repo.has_enabled_kb("acme", "sup")

    await repo.close()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])
