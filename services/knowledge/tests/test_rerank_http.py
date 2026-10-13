"""The rerank opt-out through the real FastAPI app (httpx ASGI), not just retrieve(): whatever role calls
/internal/retrieve and whatever the request says, the third-party reranker is reached only when the agent's retrieval
policy has rerank=true AND the request does not turn it off."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from rag_fakes import FakeEmbedder, add_document, make_kb, make_manager

from libs.tenancy import set_caller_tenant, set_target_tenant
from services.config import auth as config_auth
from services.config import users as users_service
from services.knowledge import retrieval, retrieval_log, runtime
from services.knowledge.app import app
from services.knowledge.reranker import CohereReranker, RerankConfig
from services.knowledge.routers import retrieve as retrieve_router

ROLES = ("viewer", "agent", "supervisor", "admin", "service")
POLICIES = (True, False, None)       # None = no policy row at all (the system default, rerank off)
REQUESTS = (True, False, "absent")


class _Resolver:
    async def resolve(self, ref):
        return "sk-test"


@pytest_asyncio.fixture(loop_scope="session")
async def http_world(pool, test_service_viewer, monkeypatch):
    calls: list[dict] = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 1 - i / 10}
                                                     for i in range(len(body["documents"]))]})

    # decisive_score above 1 so the skip rule never hides a call; budgets wide so they never hide one either
    reranker = CohereReranker(
        resolver=_Resolver(), transport=httpx.MockTransport(provider),
        config=RerankConfig(decisive_score=2.0, tenant_rate=1000.0, tenant_burst=1000, platform_rate=100000.0, processes=1))
    runtime.set_reranker(reranker)
    monkeypatch.setattr(retrieve_router, "get_embedding_manager", lambda: make_manager(FakeEmbedder()))
    retrieval.clear_caches()

    slug = f"rrhttp-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    agent = dict(await pool.fetchrow("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'S') RETURNING *", tenant["id"]))
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "kb")
    await add_document(pool, tenant, kb, "Refunds", ["Refunds are processed within 30 days of the return request."])
    await add_document(pool, tenant, kb, "Shipping", ["Shipping takes 3 to 5 business days for standard delivery."])
    tokens = {}
    for role in ("viewer", "agent", "supervisor", "admin"):
        user = await users_service.create_user(
            email=f"rrhttp-{role}-{uuid.uuid4().hex[:8]}@example.com", password="test-password-not-real",
            role=role, tenant_id=tenant["id"])
        tokens[role] = config_auth.create_access_token(user)
    tokens["service"] = test_service_viewer["token"]
    set_caller_tenant(None)
    set_target_tenant(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "tokens": tokens, "slug": slug, "agent": agent, "calls": calls, "pool": pool}

    set_caller_tenant(None)
    runtime.set_reranker(None)
    await reranker.aclose()
    await retrieval_log.flush()
    for sql in (
        "DELETE FROM retrieval_logs WHERE tenant_id = $1", "DELETE FROM kb_chunks WHERE tenant_id = $1",
        "DELETE FROM kb_documents WHERE tenant_id = $1",
        "DELETE FROM agent_retrieval_policies WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        "DELETE FROM agent_knowledge_bases WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        "DELETE FROM knowledge_bases WHERE tenant_id = $1", "DELETE FROM agents WHERE tenant_id = $1",
        "DELETE FROM audit_log WHERE tenant_id = $1", "UPDATE users SET tenant_id = NULL WHERE tenant_id = $1",
        "DELETE FROM provider_configs WHERE tenant_id = $1", "DELETE FROM tenants WHERE id = $1",
    ):
        await pool.execute(sql, tenant["id"])


async def _set_policy(w, policy):
    pool, agent = w["pool"], w["agent"]
    await pool.execute("DELETE FROM agent_retrieval_policies WHERE agent_id = $1", agent["id"])
    if policy is not None:
        await pool.execute("INSERT INTO agent_retrieval_policies (agent_id, rerank) VALUES ($1, $2)", agent["id"], policy)
    retrieval.clear_caches()


@pytest.mark.parametrize("policy", POLICIES)
async def test_the_provider_is_reached_only_when_the_policy_is_true_and_the_request_does_not_turn_it_off(http_world, policy):
    w = http_world
    await _set_policy(w, policy)
    denied: set[str] = set()
    for role in ROLES:
        for request_flag in REQUESTS:
            before = len(w["calls"])
            body = {"tenant_slug": w["slug"], "agent_slug": "sup", "query": "refund shipping days", "minimum_score": 0.0}
            if request_flag != "absent":
                body["rerank"] = request_flag
            resp = await w["client"].post("/internal/retrieve", json=body, headers={"Authorization": f"Bearer {w['tokens'][role]}"})
            if resp.status_code == 403:  # a role the knowledge service refuses outright: certainly no provider call
                assert len(w["calls"]) == before, (role, policy, request_flag)
                denied.add(role)
                continue
            assert resp.status_code == 200, (role, resp.text)
            result = resp.json()
            assert result["status"] == "ok", (role, policy, request_flag, result)
            expected = policy is True and request_flag is not False
            sent = len(w["calls"]) - before
            assert sent == (1 if expected else 0), f"role={role} policy={policy} request={request_flag}: {sent} provider calls"
            assert result["retrieval_metadata"]["rerank"] is expected
            if not expected:
                assert "rerank_skipped" not in result["degraded"]  # not even attempted: no skip to report
                assert result["retrieval_metadata"]["timings_ms"].get("rerank") is None
    assert {"viewer", "admin", "service"}.isdisjoint(denied)  # the roles that matter really were exercised


async def test_only_query_and_chunk_text_reach_the_provider_over_http(http_world):
    w = http_world
    await _set_policy(w, True)
    w["calls"].clear()
    resp = await w["client"].post(
        "/internal/retrieve", json={"tenant_slug": w["slug"], "agent_slug": "sup", "query": "refund shipping days", "minimum_score": 0.0},
        headers={"Authorization": f"Bearer {w['tokens']['viewer']}"})
    assert resp.status_code == 200 and len(w["calls"]) == 1
    sent = w["calls"][0]
    assert set(sent) == {"model", "query", "documents", "top_n"} and sent["query"] == "refund shipping days"
    assert all(isinstance(d, str) for d in sent["documents"]) and w["slug"] not in json.dumps(sent)
