"""Hosted-embedding budget and keep-alive: per-engine timeout defaults, persistent HTTP clients, warm-up, shutdown,
and a hosted-engine query at 250 ms embed latency that must still get dense hits."""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from fake_embed_server import FakeEmbedServer
from rag_fakes import add_document, make_kb

from libs.tenancy import set_caller_tenant
from services.knowledge import db, runtime
from services.knowledge.embedding_manager import (
    EmbeddingProviderConfig, EmbeddingProviderManager, OllamaEmbeddingProvider, OpenAIEmbeddingProvider,
)
from services.knowledge.resilience import (
    CircuitBreaker, RetrievalConfig, resolve_embed_timeout_s,
)
from services.knowledge.retrieval import retrieve
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.vector_repository import PgVectorRepository

REFUND = "Refunds are processed within 30 days of the return request."


@pytest_asyncio.fixture(loop_scope="session")
async def server():
    srv = await FakeEmbedServer().start()
    yield srv
    await srv.stop()


# --- per-engine defaults and overrides ------------------------------------------------------------------------

def test_per_engine_defaults_local_150_hosted_300(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_EMBED_TIMEOUT_MS", raising=False)
    cfg = RetrievalConfig.from_env()
    assert resolve_embed_timeout_s("ollama", {}, cfg) == 0.150
    assert resolve_embed_timeout_s("openai", {}, cfg) == 0.300
    assert resolve_embed_timeout_s("some-other-hosted-engine", None, cfg) == 0.300


def test_env_override_is_global_and_clamped_and_garbage_is_ignored(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EMBED_TIMEOUT_MS", "500")
    cfg = RetrievalConfig.from_env()
    assert resolve_embed_timeout_s("ollama", {}, cfg) == resolve_embed_timeout_s("openai", {}, cfg) == 0.5
    monkeypatch.setenv("KNOWLEDGE_EMBED_TIMEOUT_MS", "999999")
    assert resolve_embed_timeout_s("openai", {}, RetrievalConfig.from_env()) == 2.0
    for garbage in ("abc", "", "nan", "-inf"):
        monkeypatch.setenv("KNOWLEDGE_EMBED_TIMEOUT_MS", garbage)
        cfg = RetrievalConfig.from_env()
        assert resolve_embed_timeout_s("ollama", {}, cfg) == 0.150 and resolve_embed_timeout_s("openai", {}, cfg) == 0.300


@pytest.mark.parametrize("raw, expected", [
    (100, 0.1), ("250", 0.25), (7, 0.05), (-5, 0.05), (99999, 2.0), (1500.5, 1.5005),  # clamped to 50..2000 ms
    (True, 0.3), (None, 0.3), ("fast", 0.3), (float("nan"), 0.3), ([1], 0.3),  # ignored: engine default
])
def test_per_config_extra_override_is_clamped_and_parsed_defensively(raw, expected):
    assert resolve_embed_timeout_s("openai", {"embed_timeout_ms": raw}, RetrievalConfig()) == pytest.approx(expected)


def test_a_per_config_value_beats_the_global_env_value():
    cfg = RetrievalConfig(embed_timeout_override_s=0.5)
    assert resolve_embed_timeout_s("openai", {"embed_timeout_ms": 120}, cfg) == 0.12


def test_hosted_budget_does_not_turn_slow_calls_into_an_outage_storm():
    """Breaker: 5 CONSECUTIVE failures open it. At a 300 ms budget with ~5% of calls late, four in a row then a
    success never opens it; a real outage (5 in a row) does."""
    cfg = RetrievalConfig()
    assert cfg.breaker_failures == 5
    b = CircuitBreaker(cfg.breaker_failures, cfg.breaker_cooldown_s)
    for _ in range(4):
        b.record_failure()
    b.record_success()
    for _ in range(4):
        b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open"


# --- persistent keep-alive client -----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["openai", "ollama"])
async def test_n_embeds_reuse_one_connection(server, kind):
    provider = (OpenAIEmbeddingProvider("k", base_url=server.openai_base) if kind == "openai"
                else OllamaEmbeddingProvider(base_url=server.ollama_base))
    before = server.connections
    try:
        for i in range(10):
            [vec] = await provider.embed([f"refund question {i}"])
            assert len(vec) == 768
    finally:
        await provider.aclose()
    assert server.connections - before == 1  # one TCP connection for ten calls (was ten)


async def test_a_closed_client_is_recreated_on_the_next_call(server):
    provider = OpenAIEmbeddingProvider("k", base_url=server.openai_base)
    before = server.connections
    await provider.embed(["a"])
    await provider.aclose()
    assert provider._client is None
    await provider.embed(["b"])  # re-created, not an error
    await provider.aclose()
    assert server.connections - before == 2
    await provider.embed(["c"])
    await provider._client.aclose()  # closed behind the provider's back (a shutdown race): still recovers
    await provider.embed(["d"])
    await provider.aclose()


async def test_warm_up_opens_the_connection_without_credentials_or_text(server):
    manager = EmbeddingProviderManager(CompositeSecretResolver(), registry={
        "openai": lambda cfg, key: _async(OpenAIEmbeddingProvider("SECRET-KEY", base_url=server.openai_base))})
    cfg = EmbeddingProviderConfig(id="warm-cfg", engine="openai")
    before_c, before_h, before_r = server.connections, server.heads, server.requests
    await manager.warm(cfg)
    assert (server.connections - before_c, server.heads - before_h, server.requests - before_r) == (1, 1, 0)
    assert "authorization" not in server.head_headers[-1]  # no credentials on the warm-up
    provider = await manager.get(cfg)
    await provider.embed(["real query"])
    assert server.connections - before_c == 1  # the first real embed rode the warm connection
    await manager.aclose()


async def _async(value):
    return value


async def test_manager_close_closes_every_providers_client(server):
    providers = [OpenAIEmbeddingProvider("k", base_url=server.openai_base), OllamaEmbeddingProvider(base_url=server.ollama_base)]
    manager = EmbeddingProviderManager(CompositeSecretResolver(), registry={
        "openai": lambda cfg, key: _async(providers[0]), "ollama": lambda cfg, key: _async(providers[1])})
    for i, engine in enumerate(("openai", "ollama")):
        await (await manager.get(EmbeddingProviderConfig(id=f"c{i}", engine=engine))).embed(["x"])
    assert all(p._client is not None and not p._client.is_closed for p in providers)
    clients = [p._client for p in providers]
    await manager.aclose()
    assert all(c.is_closed for c in clients)


async def test_app_shutdown_closes_embedding_clients_before_the_pool(server, monkeypatch):
    from services.knowledge.app import app
    provider = OpenAIEmbeddingProvider("k", base_url=server.openai_base)
    manager = EmbeddingProviderManager(CompositeSecretResolver(), registry={"openai": lambda cfg, key: _async(provider)})
    monkeypatch.setattr(runtime, "_embedding_manager", manager)
    seen = {}

    async def fake_close_pool():
        seen["client_at_pool_close"] = provider._client

    monkeypatch.setattr(db, "close_pool", fake_close_pool)
    async with app.router.lifespan_context(app):
        await (await manager.get(EmbeddingProviderConfig(id="life", engine="openai"))).embed(["x"])
        assert provider._client is not None
    assert seen == {"client_at_pool_close": None}


# --- retrieval with a hosted engine ---------------------------------------------------------------------------

async def _hosted_world(pool, tenant_agent, server):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "hosted")
    await pool.execute("UPDATE provider_configs SET engine = 'openai' WHERE id = $1", kb["embedding_config_id"])
    await add_document(pool, tenant, kb, "Refund Policy", [REFUND])
    provider = OpenAIEmbeddingProvider("k", base_url=server.openai_base)
    manager = EmbeddingProviderManager(CompositeSecretResolver(), registry={"openai": lambda cfg, key: _async(provider)})
    return tenant, agent, kb, manager


async def _ask(tenant, agent, manager, **kw):
    set_caller_tenant(str(tenant["id"]))
    return await retrieve(None, PgVectorRepository(), manager, tenant_slug=tenant["slug"], agent_slug=agent["slug"],
                          query="refunds processed 30 days return request", **kw)


async def test_a_hosted_engine_at_250ms_embed_latency_still_gets_dense_hits(pool, tenant_agent, server):
    tenant, agent, kb, manager = await _hosted_world(pool, tenant_agent, server)
    server.latency_s = 0.250
    try:
        t0 = time.monotonic()
        result = await _ask(tenant, agent, manager)
        elapsed = time.monotonic() - t0
        assert result["status"] == "ok", result["degraded"]  # not lexical-only
        assert result["retrieval_metadata"]["dense_candidates"] >= 1
        assert not {"embedding_unavailable", "lexical_only"} & set(result["degraded"])
        assert elapsed < 0.4
    finally:
        server.latency_s = 0.0
        await manager.aclose()


async def test_per_config_override_and_the_deadline_hold_when_the_provider_is_slower_than_its_budget(pool, tenant_agent, server):
    tenant, agent, kb, manager = await _hosted_world(pool, tenant_agent, server)
    server.latency_s = 0.250
    try:
        await pool.execute("UPDATE provider_configs SET extra = '{\"embed_timeout_ms\": 100}'::jsonb WHERE id = $1",
                           kb["embedding_config_id"])
        from services.knowledge import retrieval
        retrieval.clear_caches()
        result = await _ask(tenant, agent, manager)
        assert {"embedding_unavailable", "lexical_only"} <= set(result["degraded"])  # the 100 ms override applied
        server.latency_s = 0.600  # slower than even the 300 ms default
        await pool.execute("UPDATE provider_configs SET extra = '{}'::jsonb WHERE id = $1", kb["embedding_config_id"])
        retrieval.clear_caches()
        t0 = time.monotonic()
        result = await _ask(tenant, agent, manager)
        assert time.monotonic() - t0 < 0.43 and "lexical_only" in result["degraded"]  # 400 ms deadline holds
    finally:
        server.latency_s = 0.0
        await manager.aclose()
