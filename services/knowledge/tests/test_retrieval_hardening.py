"""Review/security fixes on the retrieval hot path: cache-key collisions, admission and pool starvation, query
caps, log retention and redaction, plan staleness, parent-join guard, deadline overruns, score scales, breaker
logging, shutdown, token-budget reservation."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from rag_fakes import FakeEmbedder, add_document, embed_text, make_kb, make_manager

from libs.tenancy import TenantScopeConflict, set_caller_tenant, set_target_tenant, tenant_conn
from services.knowledge import cache, db, retrieval, retrieval_log
from services.knowledge import documents as documents_service
from services.knowledge import retrieval_plan as plans
from services.knowledge import vector_repository as vr
from services.knowledge.resilience import (
    AdmissionLimiter, CircuitBreaker, RetrievalConfig, RetrievalUnavailable, breakers,
)
from services.knowledge.retrieval import retrieve
from services.knowledge.schemas import RetrieveRequest
from services.knowledge.vector_repository import PgVectorRepository

def _load_tripwire():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "tests" / "test_rls_coverage.py"
    spec = importlib.util.spec_from_file_location("rls_coverage_for_hardening", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._is_tenant_safe_cache_key


_is_tenant_safe = _load_tripwire()
REFUND = "Refunds are processed within 30 days of the return request."


async def _run(tenant, agent, provider, query, *, repo=None, held=None, **kw):
    set_caller_tenant(str(tenant["id"]))
    return await retrieve(
        held, repo or PgVectorRepository(), make_manager(provider),
        tenant_slug=tenant["slug"], agent_slug=agent["slug"], query=query, **kw,
    )


async def _make_tenant(pool, tenant_slug, agent_slug, prompt_text, *, embedding=False):
    """A tenant + agent + KB holding one prompt-mode document; returns (tenant, agent, cleanup)."""
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", tenant_slug))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, $2, 'A') RETURNING *", tenant["id"], agent_slug))
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "kb", embedding=embedding)
    await add_document(pool, tenant, kb, "Doc", [prompt_text], usage_mode="prompt", embed=False)

    async def cleanup():
        await retrieval_log.flush()
        await pool.execute("DELETE FROM retrieval_logs WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    return tenant, agent, cleanup


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "policies")
    await add_document(pool, tenant, kb, "Refund Policy", [REFUND])
    return {"tenant": tenant, "agent": agent, "kb": kb, "embedder": FakeEmbedder()}


# === 1. plan-cache key collision (CRITICAL) =====================================================================

def test_slug_keys_are_injective_and_still_tenant_safe():
    pairs = [("acme", "support:bot"), ("acme:support", "bot")]
    assert cache.plan_key(*pairs[0]) != cache.plan_key(*pairs[1])
    assert cache.slug_key("agent_kb", *pairs[0]) != cache.slug_key("agent_kb", *pairs[1])
    from services.knowledge.cache import _agent_kb_key
    assert _agent_kb_key(*pairs[0]) != _agent_kb_key(*pairs[1])
    for t, a in pairs:  # still passes the repo's tenant-safe tripwire (tenant slug appears as ":{tenant}:")
        assert _is_tenant_safe(cache.plan_key(t, a), tenant_prefix=t)


async def test_colliding_slug_pairs_never_cross_serve_under_the_conversation_service_scope(pool):
    """tenant 'acme-x' + agent 'support:bot'  vs  tenant 'acme-x:support' + agent 'bot': one naive key."""
    base = f"acme-{uuid.uuid4().hex[:6]}"
    t1, a1, clean1 = await _make_tenant(pool, base, "support:bot", "TENANT-ONE-SECRET")
    t2, a2, clean2 = await _make_tenant(pool, f"{base}:support", "bot", "TENANT-TWO-SECRET")
    try:
        # The Conversation service account is platform-scoped: no caller tenant, only the router-set target.
        for first, second in ((t1, t2), (t2, t1)):
            retrieval.clear_caches()
            for t, a, expected in [(first, a1 if first is t1 else a2, None), (second, a1 if second is t1 else a2, None)]:
                set_caller_tenant(None)
                set_target_tenant(t["slug"])
                result = await retrieve(None, PgVectorRepository(), make_manager(FakeEmbedder()),
                                        tenant_slug=t["slug"], agent_slug=a["slug"], query="anything")
                want = "TENANT-ONE-SECRET" if t is t1 else "TENANT-TWO-SECRET"
                other = "TENANT-TWO-SECRET" if t is t1 else "TENANT-ONE-SECRET"
                texts = [c["content"] for c in result["chunks"]]
                assert want in texts and other not in texts, (t["slug"], texts)
    finally:
        set_caller_tenant(None)
        set_target_tenant(None)
        await clean1()
        await clean2()


def test_a_plan_cached_under_the_right_key_for_other_slugs_is_not_served():
    plan = plans.RetrievalPlan(
        tenant_slug="t-a", agent_slug="x", tenant_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()),
        agent_policy={}, kb_ids=[], groups={}, embedding_configs={}, prompt_matches=[])
    plans._plans.set(cache.plan_key("t-b", "x"), plan)  # wrong slugs under t-b's key
    set_caller_tenant(None)
    set_target_tenant(None)
    assert plans.cached_plan("t-b", "x") is None


async def test_cold_path_scope_conflict_is_an_error_not_a_partial_result(world, other_tenant_admin):
    """Reviewer repro: a tenant-A caller asking with tenant B's slugs got B's prompt doc back as 'partial'."""
    t, a = world["tenant"], world["agent"]
    set_caller_tenant(str(other_tenant_admin["tenant"]["id"]))
    with pytest.raises(TenantScopeConflict):  # superuser pool: RLS does not hide the agent, the scope check must
        await retrieve(None, PgVectorRepository(), make_manager(world["embedder"]),
                       tenant_slug=t["slug"], agent_slug=a["slug"], query="refunds")
    # Nothing was cached for the wrong caller, and the owner is unaffected.
    assert plans.cached_plan(t["slug"], a["slug"]) is None
    assert (await _run(t, a, world["embedder"], "refunds processed days"))["status"] == "ok"


async def test_hit_path_checks_the_router_set_target_when_there_is_no_caller(world):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")
    set_caller_tenant(None)
    set_target_tenant("some-other-tenant")
    try:
        with pytest.raises(TenantScopeConflict):
            await retrieve(None, PgVectorRepository(), make_manager(world["embedder"]),
                           tenant_slug=t["slug"], agent_slug=a["slug"], query="refunds")
    finally:
        set_target_tenant(None)


# === 2. admission limiter and pool starvation (HIGH) ============================================================

async def test_router_shape_with_24_concurrent_retrievals_is_not_mostly_partial(pool, world):
    """The router holds a connection around retrieve(); 24 of those against a 20-connection pool."""
    t, a, embedder = world["tenant"], world["agent"], world["embedder"]
    repo = PgVectorRepository()
    for q in [f"refunds processed days {i}" for i in range(24)]:  # warm plan + embeddings
        await _run(t, a, embedder, q, repo=repo)
    await retrieval_log.flush()

    async def router_call(i):
        set_caller_tenant(str(t["id"]))
        try:
            async with tenant_conn(pool) as held:
                return (await retrieve(held, repo, make_manager(embedder), tenant_slug=t["slug"],
                                       agent_slug=a["slug"], query=f"refunds processed days {i}"))["status"]
        except RetrievalUnavailable:
            return "unavailable"

    statuses = await asyncio.gather(*[router_call(i) for i in range(24)])
    assert statuses.count("ok") >= 22, statuses
    assert statuses.count("partial") == 0, statuses


async def test_overload_is_a_typed_fast_answer(world):
    t, a = world["tenant"], world["agent"]
    cfg = RetrievalConfig(max_inflight=1, tenant_max_inflight=1, queue_wait_s=0.01)
    await _run(t, a, world["embedder"], "refunds processed days")
    slow = asyncio.create_task(_run(t, a, world["embedder"], "refunds processed days",
                                    repo=_Slow(0.3), config=cfg))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    with pytest.raises(RetrievalUnavailable) as exc:
        await _run(t, a, world["embedder"], "refunds processed days", config=cfg)
    assert time.monotonic() - t0 < 0.1 and exc.value.degraded == ["overloaded"]
    await slow


class _Slow(PgVectorRepository):
    def __init__(self, sleep):
        super().__init__()
        self.sleep = sleep

    async def search(self, conn, *a, **kw):
        await conn.execute("SELECT pg_sleep($1)", self.sleep)
        return await super().search(conn, *a, **kw)


async def test_pool_acquire_timeout_is_classified_as_overload_not_a_silent_branch_failure(pool, world):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")  # plan cached
    tiny = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], min_size=1, max_size=1)
    try:
        async with tiny.acquire():  # the pool's only connection is busy
            cfg = RetrievalConfig(acquire_timeout_s=0.02)
            with pytest.raises(RetrievalUnavailable) as exc:
                await _run(t, a, world["embedder"], "refunds processed days", pool=tiny, config=cfg)
        assert "overloaded" in exc.value.degraded
    finally:
        await tiny.close()


async def test_limiter_enforces_global_and_per_tenant_caps_fifo_and_leaks_nothing():
    lim = AdmissionLimiter()
    cfg = RetrievalConfig(max_inflight=2, tenant_max_inflight=1)
    assert await lim.acquire("a", cfg, 0)
    assert not await lim.acquire("a", cfg, 0)  # per-tenant cap
    assert await lim.acquire("b", cfg, 0)
    assert not await lim.acquire("c", cfg, 0.01)  # global cap, bounded wait, times out
    waiter = asyncio.create_task(lim.acquire("c", cfg, 1.0))
    await asyncio.sleep(0.01)
    lim.release("a")  # a slot frees: handed to the queued waiter
    assert await waiter
    lim.release("b")
    lim.release("c")
    assert lim.in_flight == 0 and lim._per == {} and lim._waiters == []


# === 3. query length cap (MEDIUM) ===============================================================================

class _Spy(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.lengths = []

    async def embed(self, texts):
        self.lengths.extend(len(t) for t in texts)
        return [embed_text("refunds processed days") for _ in texts]


async def test_a_5mb_query_is_capped_before_any_processing(world):
    t, a = world["tenant"], world["agent"]
    spy = _Spy()
    await _run(t, a, spy, "warm")
    spy.lengths.clear()
    huge = "refunds " * (5 * 1024 * 1024 // 8)
    t0 = time.monotonic()
    result = await _run(t, a, spy, huge)
    assert time.monotonic() - t0 < 0.5
    assert spy.lengths and max(spy.lengths) <= 1000
    assert result["status"] in ("ok", "partial")


def test_request_schema_rejects_oversized_queries():
    with pytest.raises(ValueError):
        RetrieveRequest(tenant_slug="t", agent_slug="a", query="x" * 4001)
    RetrieveRequest(tenant_slug="t", agent_slug="a", query="x" * 4000)


# === 4. retention + per-tenant log rate limit (MEDIUM) ==========================================================

async def _log_row(pool, tenant, agent, age_days):
    await pool.execute(
        "INSERT INTO retrieval_logs (tenant_id, agent_id, trace_id, query, status, created_at) "
        "VALUES ($1, $2, $3, 'q', 'ok', now() - make_interval(days => $4))",
        tenant["id"], agent["id"], uuid.uuid4().hex, age_days)


async def test_purge_deletes_only_rows_past_retention_in_batches(pool, world, monkeypatch):
    t, a = world["tenant"], world["agent"]
    for _ in range(5):
        await _log_row(pool, t, a, 40)
    for _ in range(3):
        await _log_row(pool, t, a, 5)
    monkeypatch.setattr(retrieval_log, "_PURGE_BATCH", 2)  # forces several batches
    deleted = await retrieval_log.purge_once(retention_days=30)
    assert deleted == 5
    assert await pool.fetchval("SELECT count(*) FROM retrieval_logs WHERE tenant_id = $1", t["id"]) == 3


def test_per_tenant_token_bucket_drops_over_limit_rows_without_blocking(monkeypatch):
    monkeypatch.setattr(retrieval_log, "_BURST", 3.0)
    monkeypatch.setattr(retrieval_log, "_RATE_PER_S", 1.0)
    retrieval_log._buckets.clear()
    assert [retrieval_log._allow("t1", now=100.0) for _ in range(5)] == [True, True, True, False, False]
    assert retrieval_log._allow("t2", now=100.0)  # another tenant has its own bucket
    assert retrieval_log._allow("t1", now=101.5)  # refilled


async def test_record_drops_and_counts_when_a_tenant_is_over_its_rate(monkeypatch):
    monkeypatch.setattr(retrieval_log, "_BURST", 1.0)
    monkeypatch.setattr(retrieval_log, "_RATE_PER_S", 0.0)
    retrieval_log._buckets.clear()

    async def noop(row):
        return None
    monkeypatch.setattr(retrieval_log, "_insert", noop)
    before = retrieval_log.dropped_rate_limited
    row = retrieval_log.RetrievalLogRow("tenant-x", None, "x", "q", "ok", [], [], None, 0, {})
    for _ in range(4):
        retrieval_log.record(row)
    assert retrieval_log.dropped_rate_limited - before == 3
    await retrieval_log.flush()


# === 5. redaction of stored queries (LOW) =======================================================================

@pytest.mark.parametrize("raw, expected", [
    ("call me at 415-555-0199 please", "call me at [digits] please"),
    ("my card is 4111 1111 1111 1111", "my card is [digits]"),
    ("order 123456", "order [digits]"),
    ("write to jane.doe+x@example.co.uk now", "write to [email] now"),
    ("I want 3 items on 12 May", "I want 3 items on 12 May"),  # short numbers are kept
])
def test_redact_query_masks_digit_runs_and_emails(raw, expected):
    assert retrieval_log.redact_query(raw) == expected


def test_redact_query_truncates_to_500():
    assert len(retrieval_log.redact_query("a" * 5000)) == 500


async def test_stored_row_is_redacted(pool, world):
    t, a = world["tenant"], world["agent"]
    result = await _run(t, a, world["embedder"], "refunds for 4111 1111 1111 1111 to bob@example.com")
    await retrieval_log.flush()
    stored = await pool.fetchval("SELECT query FROM retrieval_logs WHERE trace_id = $1", result["trace_id"])
    assert "4111" not in stored and "bob@" not in stored and "[digits]" in stored and "[email]" in stored


# === 6. plan staleness: document edits, stale-while-revalidate, prewarm route ===================================

async def test_deleting_or_retyping_a_prompt_document_takes_effect_immediately(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    doc = await add_document(pool, t, kb, "Rules", ["Always greet politely."], usage_mode="prompt", embed=False)
    contents = lambda r: [c["content"] for c in r["chunks"]]  # noqa: E731
    assert "Always greet politely." in contents(await _run(t, a, world["embedder"], "refunds processed days"))
    await documents_service.update_document(doc["id"], usage_mode="auto")
    assert "Always greet politely." not in contents(await _run(t, a, world["embedder"], "refunds processed days"))
    await documents_service.update_document(doc["id"], usage_mode="prompt")
    assert "Always greet politely." in contents(await _run(t, a, world["embedder"], "refunds processed days"))
    await documents_service.soft_delete_document(doc["id"])
    assert "Always greet politely." not in contents(await _run(t, a, world["embedder"], "refunds processed days"))


async def test_stale_plan_is_served_while_one_background_refresh_runs(world, monkeypatch):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")
    key = cache.plan_key(t["slug"], a["slug"])
    plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S  # older than TTL/2, younger than the hard TTL
    loads = []
    original = plans.load_plan

    async def counting(conn, *args):
        loads.append(1)
        await asyncio.sleep(0.05)
        return await original(conn, *args)
    monkeypatch.setattr(plans, "load_plan", counting)
    results = await asyncio.gather(*[_run(t, a, world["embedder"], "refunds processed days") for _ in range(5)])
    assert all("plan_cache_miss" not in r["degraded"] for r in results)  # nobody paid for a cold plan
    await asyncio.sleep(0.2)
    assert len(loads) == 1  # a single bounded refresh, not one per request
    assert time.monotonic() - plans._loaded_at[key] < 1.0 and not plans._refreshing


async def test_invalidation_during_a_refresh_is_not_overwritten_by_the_stale_load(world, monkeypatch):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")
    key = cache.plan_key(t["slug"], a["slug"])
    plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S
    original = plans.load_plan

    async def slow(conn, *args):
        plan = await original(conn, *args)
        await asyncio.sleep(0.1)
        return plan
    monkeypatch.setattr(plans, "load_plan", slow)
    await _run(t, a, world["embedder"], "refunds processed days")  # starts the refresh
    plans.invalidate_agent(t["slug"], a["slug"])  # lands while it is loading
    await asyncio.sleep(0.25)
    assert plans.cached_plan(t["slug"], a["slug"]) is None


async def test_prewarm_route_warms_for_the_tenant_and_refuses_foreign_tenants(pool, test_tenant, test_admin, other_tenant_admin):
    from services.knowledge.app import app
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'pw', 'PW') RETURNING *", test_tenant["id"]))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await client.post("/internal/prewarm", json={"tenant_slug": test_tenant["slug"], "agent_slug": "pw"},
                                   headers={"Authorization": f"Bearer {test_admin['token']}"})
            assert ok.status_code == 200 and ok.json() == {"warmed": True}
            assert plans.cached_plan(test_tenant["slug"], "pw") is not None
            foreign = await client.post("/internal/prewarm",
                                        json={"tenant_slug": test_tenant["slug"], "agent_slug": "pw"},
                                        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"})
            assert foreign.status_code in (403, 404)
    finally:
        await pool.execute("DELETE FROM agents WHERE id = $1", agent["id"])


# === 7. parent join guard (LOW) =================================================================================

async def test_a_parent_pointing_into_another_document_or_tenant_is_never_returned(pool, world, other_tenant_admin):
    t, kb = world["tenant"], world["kb"]
    other = other_tenant_admin["tenant"]
    foreign_kb = dict(await pool.fetchrow(
        "INSERT INTO knowledge_bases (tenant_id, slug, name) VALUES ($1, 'f', 'F') RETURNING *", other["id"]))
    own_parent_doc = await add_document(pool, t, kb, "Own other doc", ["unrelated filler words"],
                                        parent="OTHER-DOC-PARENT-SECRET text")
    foreign_doc = await add_document(pool, other, foreign_kb, "Foreign", ["foreign filler"],
                                     parent="OTHER-TENANT-PARENT-SECRET text")
    try:
        victim = await add_document(pool, t, kb, "Victim", ["Gizmo warranty covers two years."])
        for secret_parent in (own_parent_doc["parent_id"], foreign_doc["parent_id"]):
            await pool.execute("UPDATE kb_chunks SET parent_id = $1 WHERE id = $2", secret_parent, victim["chunk_ids"][0])
            repo = PgVectorRepository()
            async with tenant_conn(pool) as conn:
                dense = await repo.search(conn, [str(kb["id"])], embed_text("Gizmo warranty covers two years"), 5, 0.0)
                lexical = await repo.lexical_search(conn, [str(kb["id"])], "gizmo warranty", 5, 0.0)
            texts = [m.content for m in dense + lexical]
            assert "Gizmo warranty covers two years." in texts
            assert not any("SECRET" in x for x in texts), texts
    finally:
        await pool.execute("UPDATE kb_chunks SET parent_id = NULL WHERE document_id IN ($1, $2)",
                           foreign_doc["id"], own_parent_doc["id"])
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other["id"])


# === 8. deadline: fallbacks use min(stage timeout, remaining) ===================================================

async def test_lexical_fallback_after_a_late_dense_failure_cannot_overrun_the_deadline(world, monkeypatch):
    t, a = world["tenant"], world["agent"]
    original = plans.load_plan

    async def slow_plan(conn, *args):
        await asyncio.sleep(0.33)  # leaves ~70 ms of the 400 ms deadline
        return await original(conn, *args)
    monkeypatch.setattr(plans, "load_plan", slow_plan)

    class SlowLexical(PgVectorRepository):
        async def lexical_search(self, conn, *a_, **kw):
            await conn.execute("SELECT pg_sleep(1)")
            return await super().lexical_search(conn, *a_, **kw)

    t0 = time.monotonic()  # production timeouts (RetrievalConfig defaults)
    try:
        result = await _run(t, a, FakeEmbedder(fail=True), "refunds processed days", hybrid_search=False,
                            repo=SlowLexical())
        status = result["status"]
    except RetrievalUnavailable:
        status = "unavailable"
    elapsed = time.monotonic() - t0
    assert status in ("partial", "unavailable")
    assert elapsed < 0.43, elapsed  # was ~0.47 when the fallback used the full 120 ms lexical timeout


# === 9. score scales and low_confidence =========================================================================

async def test_minimum_score_gates_dense_only_and_weak_lexical_evidence_is_low_confidence(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    await add_document(pool, t, kb, "Catalogue", ["Part number ZX9000X ships in blue."], embed=False)
    result = await _run(t, a, world["embedder"], "ZX9000X", minimum_score=0.9)
    assert result["status"] == "ok" and result["chunks"][0]["content"].startswith("Part number ZX9000X")
    assert result["low_confidence"] is True  # one lexical term, no dense support
    assert result["chunks"][0]["score"] < 0.3


async def test_strong_dense_support_is_not_low_confidence_even_with_a_single_candidate(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], REFUND)
    assert len(result["chunks"]) == 1 and result["low_confidence"] is False


# === 10. lexical cost: common terms ============================================================================

async def test_terms_too_common_in_a_big_kb_are_dropped_before_ranking(pool, world, monkeypatch):
    t, kb = world["tenant"], world["kb"]
    monkeypatch.setattr(vr, "_MAX_TERM_DF", 5)
    for i in range(8):
        await add_document(pool, t, kb, f"Common {i}", [f"widget boilerplate text number {i}"], embed=False)
    await add_document(pool, t, kb, "Rare", ["widget with the zorblax feature"], embed=False)
    repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        mixed = await repo.lexical_search(conn, [str(kb["id"])], "widget zorblax", 10, 0.0, approx_chunks=100000)
        only_common = await repo.lexical_search(conn, [str(kb["id"])], "widget boilerplate", 10, 0.0,
                                                approx_chunks=100000)
        small_kb = await repo.lexical_search(conn, [str(kb["id"])], "widget boilerplate", 10, 0.0, approx_chunks=100)
    assert [m.document_title for m in mixed] == ["Rare"]  # 'widget' (df 9 >= 5) dropped, 'zorblax' kept
    assert only_common == []  # nothing discriminating: dense answers such queries
    assert len(small_kb) >= 8  # small KBs are not probed: ranking them is cheap


# === 11. observability: embed failures and breaker transitions ==================================================

async def test_embed_failures_and_breaker_transitions_are_logged_without_tenant_data(world, caplog):
    caplog.set_level(logging.WARNING)
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=0.05)
    dead = FakeEmbedder(fail=True)
    secret = "supersecretcustomerphrase"
    for q in (f"{secret} one", f"{secret} two", f"{secret} three"):
        await _run(world["tenant"], world["agent"], dead, q, config=cfg)
    await asyncio.sleep(0.06)
    dead.fail = False
    await _run(world["tenant"], world["agent"], dead, "refunds processed days", config=cfg)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "embedding failed" in text and "circuit breaker opened" in text
    assert "circuit breaker half-open" in text and "circuit breaker closed" in text
    assert text.count("embedding failed") == 1  # rate-limited, not one line per failure
    assert secret not in text and "refund" not in text.lower()
    assert str(world["tenant"]["id"]) not in text and world["tenant"]["slug"] not in text


def test_breaker_logs_transitions_by_name(caplog):
    caplog.set_level(logging.WARNING)
    now = [0.0]
    b = CircuitBreaker(1, 5, clock=lambda: now[0], name="rerank")
    b.record_failure()
    now[0] = 6
    assert b.allow()
    b.record_success()
    assert [r.getMessage() for r in caplog.records] == [
        "circuit breaker opened dependency=rerank consecutive_failures=1",
        "circuit breaker half-open dependency=rerank",
        "circuit breaker closed dependency=rerank",
    ]


# === 12. log worker shutdown ===================================================================================

async def test_shutdown_drains_then_stops_the_worker_and_runs_before_the_pool_closes(pool, world, monkeypatch):
    from services.knowledge.app import app
    t, a = world["tenant"], world["agent"]
    result = await _run(t, a, world["embedder"], "refunds processed days")
    await retrieval_log.shutdown()
    assert retrieval_log._worker is None
    assert await pool.fetchval("SELECT count(*) FROM retrieval_logs WHERE trace_id = $1", result["trace_id"]) == 1

    seen = {}

    async def fake_close_pool():
        seen["worker_at_close"] = retrieval_log._worker  # must already be stopped
    monkeypatch.setattr(db, "close_pool", fake_close_pool)
    async with app.router.lifespan_context(app):
        await _run(t, a, world["embedder"], "refunds processed days")  # (re)starts the worker
        assert retrieval_log._worker is not None
    assert seen == {"worker_at_close": None}


# === 15. token budget: prompt docs cannot crowd out the answer ==================================================

async def test_prompt_docs_beyond_half_the_budget_are_trimmed_and_the_best_match_still_returns(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    # 48 + 47 prompt tokens of a 100-token budget: without the half-budget cap they leave 5 tokens for a 10-token answer.
    await add_document(pool, t, kb, "Big prompt A", [" ".join(f"a{i}" for i in range(48))], usage_mode="prompt", embed=False)
    await add_document(pool, t, kb, "Big prompt B", [" ".join(f"b{i}" for i in range(47))], usage_mode="prompt", embed=False)
    result = await _run(t, a, world["embedder"], "refunds processed days", max_tokens=100)
    contents = [c["content"] for c in result["chunks"]]
    assert REFUND in contents  # the answer reaches the client although prompt docs alone exceed max_tokens
    assert result["retrieval_metadata"]["always_included"] == 1
    assert result["retrieval_metadata"]["prompt_docs_trimmed"] == 1
    assert "prompt_docs_trimmed" in result["degraded"]
    assert result["token_count"] <= 100


async def test_a_lone_oversized_prompt_doc_is_truncated_not_dropped_and_the_top_match_is_truncated_not_dropped(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    await add_document(pool, t, kb, "Huge prompt", [" ".join(f"p{i}" for i in range(80))], usage_mode="prompt",
                       embed=False)
    long_answer = "Refunds processed " + " ".join(f"detail{i}" for i in range(80))
    await add_document(pool, t, kb, "Long", [long_answer])
    result = await _run(t, a, world["embedder"], "detail1 detail2 detail3 detail4 detail5",
                        max_tokens=20, minimum_score=0.0)
    texts = [c["content"] for c in result["chunks"]]
    assert len(texts[0].split()) == 10  # prompt doc cut to half the budget
    assert len(texts) == 2 and texts[1].startswith("Refunds processed") and len(texts[1].split()) == 10  # truncated, not dropped
    assert result["token_count"] <= 20
