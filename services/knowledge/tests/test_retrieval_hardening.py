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

    # Generous queue and deadline: this checks pool starvation (no partial), not how fast this machine is.
    cfg = RetrievalConfig(queue_wait_s=2.0, deadline_s=5.0)

    async def router_call(i):
        set_caller_tenant(str(t["id"]))
        try:
            async with tenant_conn(pool) as held:
                return (await retrieve(held, repo, make_manager(embedder), tenant_slug=t["slug"],
                                       agent_slug=a["slug"], query=f"refunds processed days {i}", config=cfg))["status"]
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


async def test_limiter_enforces_global_and_per_tenant_caps_and_leaks_nothing():
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
    _assert_limiter_idle(lim)


def _assert_limiter_idle(lim):
    assert lim.in_flight == 0 and lim._per == {} and lim._queues == {} and not lim._order


async def test_cancel_before_the_slot_is_handed_over_leaks_nothing():
    lim = AdmissionLimiter()
    cfg = RetrievalConfig(max_inflight=1, tenant_max_inflight=1)
    assert await lim.acquire("a", cfg, 0)
    waiter = asyncio.create_task(lim.acquire("b", cfg, 5.0))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    lim.release("a")
    assert lim.in_flight == 0
    _assert_limiter_idle(lim)


async def test_cancel_after_the_slot_was_handed_over_gives_it_back():
    """release() hands the slot to the queued waiter synchronously; the waiter is cancelled before its task runs."""
    lim = AdmissionLimiter()
    cfg = RetrievalConfig(max_inflight=1, tenant_max_inflight=1)
    assert await lim.acquire("a", cfg, 0)
    waiter = asyncio.create_task(lim.acquire("b", cfg, 5.0))
    await asyncio.sleep(0)
    lim.release("a")  # grant: in_flight == 1, owned by the (not yet resumed) waiter
    assert lim.in_flight == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert lim.in_flight == 0  # the permit came back; before the fix it stayed at 1 forever
    _assert_limiter_idle(lim)
    assert await lim.acquire("c", cfg, 0)  # and the limiter still works
    lim.release("c")


async def test_limiter_never_leaks_under_1000_random_interleavings_of_grant_expiry_and_cancel():
    import random
    rng = random.Random(1234)
    lim = AdmissionLimiter()
    for _ in range(1000):
        cfg = RetrievalConfig(max_inflight=rng.randint(1, 3), tenant_max_inflight=rng.randint(1, 2))

        async def worker(key, hold, wait):
            if await lim.acquire(key, cfg, wait):
                try:
                    await asyncio.sleep(hold)
                finally:
                    lim.release(key)

        tasks = [asyncio.create_task(worker(rng.choice("abc"), rng.choice([0, 0, 0.001, 0.003]),
                                            rng.choice([0, 0.001, 0.004, 0.02]))) for _ in range(rng.randint(2, 8))]
        for task in tasks:  # cancel some at random points: before grant, on the grant boundary, while holding
            if rng.random() < 0.5:
                await asyncio.sleep(rng.choice([0, 0, 0.0005, 0.002]))
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _assert_limiter_idle(lim)


async def test_noisy_tenant_does_not_block_other_tenants_while_global_slots_are_free():
    lim = AdmissionLimiter()
    cfg = RetrievalConfig(max_inflight=5, tenant_max_inflight=3, queue_wait_s=0.15)
    for _ in range(3):
        assert await lim.acquire("noisy", cfg, 0)  # tenant A at its cap of 3
    queued = [asyncio.create_task(lim.acquire("noisy", cfg, 1.0)) for _ in range(10)]
    await asyncio.sleep(0.01)  # A has waiters queued
    t0 = time.monotonic()
    for other in "BCDEFGH"[:2]:  # 2 global slots remain
        assert await lim.acquire(other, cfg, 0)  # admitted immediately, no queueing behind A's waiters
    assert time.monotonic() - t0 < 0.01
    assert not await lim.acquire("H", cfg, 0)  # now the global cap is the limit, not A's queue
    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    for _ in range(3):
        lim.release("noisy")
    for other in "BC":
        lim.release(other)
    _assert_limiter_idle(lim)


async def test_freed_slots_are_shared_round_robin_across_tenants():
    lim = AdmissionLimiter()
    cfg = RetrievalConfig(max_inflight=2, tenant_max_inflight=5)
    held = ["a", "a"]
    for key in held:
        assert await lim.acquire(key, cfg, 0)
    granted = []

    async def want(key):
        if await lim.acquire(key, cfg, 1.0):
            granted.append(key)

    tasks = [asyncio.create_task(want(k)) for k in ("a", "a", "b", "c")]
    await asyncio.sleep(0.01)
    for _ in range(3):
        lim.release(held.pop(0))
        await asyncio.sleep(0.01)
        held.append(granted[-1])
    assert granted[:3] == ["a", "b", "c"], granted  # plain FIFO would give a, a, b: a's backlog may not crowd out c
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for key in held + granted[3:]:
        lim.release(key)
    _assert_limiter_idle(lim)


def test_default_caps_follow_the_pool_size(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DB_POOL_MAX", "20")
    cfg = RetrievalConfig.from_env()
    assert (cfg.max_inflight, cfg.tenant_max_inflight) == (9, 3)  # (20 - 2) // 2 and a third of it
    monkeypatch.setenv("KNOWLEDGE_DB_POOL_MAX", "4")
    cfg = RetrievalConfig.from_env()
    assert (cfg.max_inflight, cfg.tenant_max_inflight) == (2, 1)
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT", "7")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT", "5")
    cfg = RetrievalConfig.from_env()
    assert (cfg.max_inflight, cfg.tenant_max_inflight) == (7, 5)


async def test_prewarm_goes_through_admission_and_a_bounded_acquire(world):
    t, a = world["tenant"], world["agent"]
    set_caller_tenant(str(t["id"]))
    cfg = RetrievalConfig(max_inflight=1, tenant_max_inflight=1, queue_wait_s=0.01)
    from services.knowledge.resilience import admission
    assert await admission.acquire("someone", cfg, 0)  # the only slot is taken
    try:
        t0 = time.monotonic()
        with pytest.raises(RetrievalUnavailable) as exc:
            await plans.prewarm(t["slug"], a["slug"], config=cfg)
        assert time.monotonic() - t0 < 0.1 and exc.value.degraded == ["overloaded"]
    finally:
        admission.release("someone")
    tiny = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], min_size=1, max_size=1)
    try:
        async with tiny.acquire():  # pool exhausted: the acquire is bounded, not unbounded
            with pytest.raises(RetrievalUnavailable):
                await plans.prewarm(t["slug"], a["slug"], pool=tiny,
                                    config=RetrievalConfig(acquire_timeout_s=0.02))
    finally:
        await tiny.close()
    from services.knowledge.resilience import admission as adm
    assert adm.in_flight == 0


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
    assert all(r["retrieval_metadata"]["plan_cache"] == "stale" for r in results)  # nobody paid for a cold plan
    await asyncio.sleep(0.2)
    assert len(loads) == 1  # a single bounded refresh, not one per request
    assert time.monotonic() - plans._loaded_at[key] < 1.0 and not plans._refresh_tasks


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
    assert [m.document_title for m in only_common] != []  # all-common query: its 2 rarest terms, ANDed, are kept
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


# === round 3 ===================================================================================================

# --- 5. Redis flag namespace ---------------------------------------------------------------------------------

async def test_redis_flag_uses_a_new_namespace_and_has_a_ttl():
    client = cache.get_client()
    suffix = uuid.uuid4().hex[:6]
    tenant, agent = f"4{suffix}", "acme:bot"
    new_key = cache._agent_kb_key(f"acme{suffix}", "bot")
    legacy = f"agent_kb:{tenant}:{agent}"
    try:
        await client.set(legacy, "1")  # what the old writer left behind (own key, own suffix)
        assert not new_key.startswith("agent_kb:")  # the old tenant/agent pair can never be byte-identical now
        await cache.set_has_enabled_kb(f"acme{suffix}", "bot", True)
        assert await cache.get_has_enabled_kb(f"acme{suffix}", "bot") is True
        assert 0 < await client.ttl(new_key) <= 7 * 24 * 3600
        assert await cache.get_has_enabled_kb(tenant, agent) is None  # the legacy key is never read
    finally:
        await client.delete(legacy, new_key)  # only keys this test created


async def test_legacy_purge_deletes_only_keys_under_its_prefix_in_the_client_it_is_given():
    import redis.asyncio as aioredis
    own = aioredis.from_url("redis://localhost:6379/13", decode_responses=True)  # test-only database
    prefix = f"legacytest{uuid.uuid4().hex[:8]}:"
    mine, other = [f"{prefix}{i}" for i in range(3)], f"othertest{uuid.uuid4().hex[:8]}:x"
    try:
        await own.mset({k: "1" for k in mine} | {other: "keep"})
        assert await cache.purge_legacy_keys(client=own, prefix=prefix) == 3
        assert [await own.exists(k) for k in mine] == [0, 0, 0] and await own.get(other) == "keep"
    finally:
        await own.delete(*mine, other)
        await own.aclose()


async def test_the_production_purge_default_is_exactly_the_legacy_prefix():
    """A stub client: nothing real is touched. Only keys starting with 'agent_kb:' may reach delete()."""
    deleted = []

    class Stub:
        async def scan_iter(self, match, count):
            assert match == "agent_kb:*"
            for key in ("agent_kb:t:a", "agent_kbx:t:a", "kbflag2:1:t:a", "xagent_kb:t:a", "agent_kb:"):
                yield key  # a glob/SCAN that over-matches must still be filtered by the exact prefix

        async def delete(self, *keys):
            deleted.extend(keys)
            return len(keys)

    assert await cache.purge_legacy_keys(client=Stub()) == 2
    assert sorted(deleted) == ["agent_kb:", "agent_kb:t:a"]


# --- 6. redaction ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "call 415.555.0199 now", "call 415,555,0199 now", "call 415/555/0199", "call 415_555_0199", "call 415  555  0199",
    "call 415 - 555 - 0199", "card 4111-1111-1111-1111", "card 4111 1111 1111 1111",
    "digits ４１５５５５０１９９",  # fullwidth
    "number ९८७६५४३२१०",  # Devanagari
    "it is four one five five five five zero one nine nine", "four, one, five, five, five, zero",
])
def test_redaction_catches_other_separators_unicode_digits_and_spoken_digits(raw):
    out = retrieval_log.redact_query(raw)
    assert "[digits]" in out, out
    assert not any(ch.isdigit() for ch in out.replace("[digits]", "")), out


def test_redaction_keeps_ordinary_numbers_and_words():
    for text in ("I need 3 tickets", "room twenty one", "one two three", "call at 5 to 6 pm"):
        assert retrieval_log.redact_query(text) == text


# --- 7. body size cap --------------------------------------------------------------------------------------

async def test_oversized_bodies_get_413_without_echo_by_header_and_while_streaming():
    from services.knowledge.app import app
    big = '{"tenant_slug":"t","agent_slug":"a","query":"' + "SECRETQUERY" * 20000 + '"}'
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/internal/retrieve", "/internal/prewarm"):
            by_header = await client.post(path, content=big, headers={"content-type": "application/json"})
            assert by_header.status_code == 413 and "SECRETQUERY" not in by_header.text

            async def stream():
                for i in range(0, len(big), 8192):
                    yield big[i:i + 8192].encode()
            streamed = await client.post(path, content=stream(), headers={"content-type": "application/json"})
            assert streamed.status_code == 413 and "SECRETQUERY" not in streamed.text
        small = await client.post("/internal/retrieve", json={"tenant_slug": "t", "agent_slug": "a", "query": "q"})
        assert small.status_code in (401, 403)  # reaches the endpoint (and auth), not the size cap


# --- 8. purge index ----------------------------------------------------------------------------------------

async def test_purge_query_can_use_the_created_at_index(pool):
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SELECT set_config('enable_seqscan', 'off', true)")
            plan = "\n".join(r[0] for r in await conn.fetch(
                "EXPLAIN SELECT id FROM retrieval_logs WHERE created_at < now() - make_interval(days => 30) LIMIT 1000"))
        finally:
            await tx.rollback()
    assert "idx_retrieval_logs_created" in plan, plan


def test_log_bucket_defaults_bound_volume_to_5_rows_a_second_burst_20():
    assert retrieval_log._RATE_PER_S == 5.0 and retrieval_log._BURST == 20.0


# --- 9/10. lexical term cache and the common-words fallback ------------------------------------------------

async def _fill_common(pool, t, kb, n=8):
    for i in range(n):
        await add_document(pool, t, kb, f"Common {i}", [f"widget gadget notes number {i}"], embed=False)


async def test_absent_and_present_term_probes_are_cached(pool, world, monkeypatch):
    t, kb = world["tenant"], world["kb"]
    await _fill_common(pool, t, kb)
    repo = PgVectorRepository()
    probes = []
    original = asyncpg.Connection.fetch

    async def spy(self, query, *args, **kw):
        if "unnest($1::text[])" in query:
            probes.append(args[0])
        return await original(self, query, *args, **kw)
    monkeypatch.setattr(asyncpg.Connection, "fetch", spy)
    async with tenant_conn(pool) as conn:
        for _ in range(3):
            await repo.lexical_search(conn, [str(kb["id"])], "widget qqqabsent", 5, 0.0, approx_chunks=100000)
    assert len(probes) == 1 and set(probes[0]) == {"widget", "qqqabsent"}  # df 0 is cached too (short TTL)
    key = f"df:{kb['id']}:qqqabsent"
    assert repo._df_absent.get(key) == 0 and repo._df.get(key) is None


async def test_a_query_of_only_common_words_falls_back_to_the_two_rarest_anded_and_says_so(pool, world, monkeypatch):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    monkeypatch.setattr(vr, "_MAX_TERM_DF", 5)
    monkeypatch.setattr(vr, "_SMALL_KB_CHUNKS", 0)  # probe even this small KB
    await _fill_common(pool, t, kb)
    repo = PgVectorRepository()
    notes = []
    async with tenant_conn(pool) as conn:
        found = await repo.lexical_search(conn, [str(kb["id"])], "widget gadget notes", 10, 0.0,
                                          approx_chunks=100000, notes=notes)
    assert len(found) == 8 and notes == ["lexical_terms_dropped"]
    # Embedder down: before this fix such a question got no chunks at all.
    result = await _run(t, a, FakeEmbedder(fail=True), "widget gadget notes", repo=PgVectorRepository())
    assert result["status"] == "partial" and len(result["chunks"]) >= 1
    assert {"lexical_only", "lexical_terms_dropped"} <= set(result["degraded"])
    # Dense up: not mislabelled no_match.
    result = await _run(t, a, world["embedder"], "widget gadget notes", repo=PgVectorRepository())
    assert result["status"] == "ok" and "lexical_terms_dropped" in result["degraded"]


# --- 11. prompt documents are ordered deterministically -----------------------------------------------------

async def test_prompt_docs_surviving_the_half_budget_cap_are_the_oldest_every_time(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    docs = []
    for i in range(4):  # inserted newest-first, so heap order is the reverse of age
        docs.append(await add_document(pool, t, kb, f"P{i}", [" ".join(f"p{i}w{j}" for j in range(30))],
                                       usage_mode="prompt", embed=False))
    for age, doc in enumerate(docs):  # P0 is the youngest, P3 the oldest
        await pool.execute("UPDATE kb_documents SET created_at = now() - make_interval(days => $1) WHERE id = $2",
                           age + 1, doc["id"])
    for _ in range(3):
        plans.clear_cache()
        result = await _run(t, a, world["embedder"], "refunds processed days", max_tokens=70)
        kept = [c["source"]["document_title"] for c in result["chunks"] if c["source"]["document_title"].startswith("P")]
        assert kept == ["P3"], kept  # cap 35 tokens: only the oldest 30-token document fits


# --- 12. background refresh tasks are tracked and stopped before the pool closes ------------------------------

async def test_shutdown_cancels_background_refreshes_and_blocks_new_ones(world, monkeypatch):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")
    key = cache.plan_key(t["slug"], a["slug"])
    plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S
    release = asyncio.Event()

    async def hanging(conn, *args):
        await release.wait()

    monkeypatch.setattr(plans, "load_plan", hanging)
    await _run(t, a, world["embedder"], "refunds processed days")  # starts the refresh
    await asyncio.sleep(0.05)
    task = plans._refresh_tasks[key]
    assert not task.done()
    try:
        await plans.shutdown()
        assert task.done() and plans._refresh_tasks == {}
        plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S
        await _run(t, a, world["embedder"], "refunds processed days")
        assert plans._refresh_tasks == {}  # closed: no new refresh may start (it could reopen the pool)
    finally:
        plans.startup()


# --- 13. invalidation is per tenant -------------------------------------------------------------------------

async def test_one_tenants_document_edit_leaves_other_tenants_plans_cached(pool, world):
    t, a, kb = world["tenant"], world["agent"], world["kb"]
    other_t, other_a, cleanup = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:6]}", "bot", "OTHER-PROMPT")
    try:
        doc = await add_document(pool, t, kb, "Edit me", ["x"], usage_mode="prompt", embed=False)
        for tenant, agent in ((t, a), (other_t, other_a)):
            set_caller_tenant(str(tenant["id"]))
            await retrieve(None, PgVectorRepository(), make_manager(FakeEmbedder()), tenant_slug=tenant["slug"],
                           agent_slug=agent["slug"], query="anything")
        set_caller_tenant(None)
        assert plans.cached_plan(t["slug"], a["slug"]) is not None
        assert plans.cached_plan(other_t["slug"], other_a["slug"]) is not None
        set_caller_tenant(str(t["id"]))
        await documents_service.update_document(doc["id"], title="Edited")
        set_caller_tenant(None)
        assert plans.cached_plan(t["slug"], a["slug"]) is None  # the editing tenant's plan is evicted...
        assert plans.cached_plan(other_t["slug"], other_a["slug"]) is not None  # ...nobody else's
    finally:
        set_caller_tenant(None)
        await cleanup()


# --- 14. the prewarm route keeps authorising the tenant -----------------------------------------------------

def test_prewarm_route_still_authorises_the_tenant_before_doing_anything():
    import inspect
    from services.knowledge.routers import prewarm
    src = inspect.getsource(prewarm.prewarm)
    assert "assert_tenant_access(body.tenant_slug, current_user)" in src
    assert src.index("assert_tenant_access") < src.index("set_target_tenant") < src.index("retrieval_plan.prewarm")


# ── SDK reader and service writer agree on the Redis flag key ───────────────

@pytest.mark.parametrize("tenant,agent", [
    ("acme", "bot"), ("acme", "support:bot"), ("acme:support", "bot"), ("4", "acme:bot"), ("", "x"), ("t", ""),
    ("ünï", "bøt"),
])
def test_sdk_redis_reader_uses_the_key_the_service_writes(tenant, agent):
    from libs.knowledge_sdk.repositories.redis_repository import agent_kb_flag_key

    assert agent_kb_flag_key(tenant, agent) == cache._agent_kb_key(tenant, agent)


# === round 4 ===================================================================================================

async def test_refused_background_refreshes_never_stay_registered(world):
    """Deployed caps (9 global slots), all taken: 20 refused refreshes must leave the registry empty and the next
    refresh must still be schedulable (it used to leak one registration per refusal, then switch refresh off)."""
    from services.knowledge.resilience import admission
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refunds processed days")
    key = cache.plan_key(t["slug"], a["slug"])
    cfg = RetrievalConfig.from_env()
    assert cfg.max_inflight == 9
    held = [f"busy{i}" for i in range(cfg.max_inflight)]
    for k in held:
        assert await admission.acquire(k, cfg, 0)
    try:
        for _ in range(20):
            plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S
            assert plans.cached_plan(t["slug"], a["slug"]) is not None  # served stale, refresh scheduled
            await asyncio.sleep(0.01)  # the task runs, is refused, and must deregister
            assert plans._refresh_tasks == {}
    finally:
        for k in held:
            admission.release(k)
    plans._loaded_at[key] = time.monotonic() - plans._PLAN_TTL_S
    plans.cached_plan(t["slug"], a["slug"])
    assert key in plans._refresh_tasks  # schedulable again
    await asyncio.sleep(0.2)
    assert plans._refresh_tasks == {} and time.monotonic() - plans._loaded_at[key] < 1.0  # and it ran
    assert admission.in_flight == 0


async def test_fallback_candidate_cap_is_applied_after_the_document_filters(pool, world, monkeypatch):
    """A soft-deleted document keeps its chunks: 2100 of them used to fill the 2000-candidate cap and hide the one
    live match for an all-common-word query."""
    t, kb = world["tenant"], world["kb"]
    monkeypatch.setattr(vr, "_MAX_TERM_DF", 5)
    monkeypatch.setattr(vr, "_SMALL_KB_CHUNKS", 0)
    dead = await add_document(pool, t, kb, "Dead", ["seed"], embed=False, deleted=True)
    await pool.executemany(
        "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count) "
        "VALUES ($1, $2, $3, $4, 'refund policy deleted words', 4)",
        [(dead["id"], kb["id"], t["id"], 1000 + i) for i in range(2100)])
    await add_document(pool, t, kb, "Live", ["refund policy live words"], embed=False)
    async with tenant_conn(pool) as conn:
        found = await PgVectorRepository().lexical_search(conn, [str(kb["id"])], "refund policy", 5, 0.0,
                                                          approx_chunks=100000)
    assert [m.document_title for m in found] == ["Live"]


async def test_the_fallback_note_is_added_even_for_a_one_or_two_word_query(pool, world, monkeypatch):
    t, kb = world["tenant"], world["kb"]
    monkeypatch.setattr(vr, "_MAX_TERM_DF", 5)
    await _fill_common(pool, t, kb)
    notes = []
    async with tenant_conn(pool) as conn:
        found = await PgVectorRepository().lexical_search(conn, [str(kb["id"])], "widget gadget", 10, 0.0,
                                                          approx_chunks=100000, notes=notes)
    assert len(found) == 8 and notes == ["lexical_terms_dropped"]  # 2 terms: nothing "dropped", but the fallback ran


async def test_pool_min_never_exceeds_max_and_idle_connections_are_kept_warm(monkeypatch):
    from services.knowledge import db as kdb
    monkeypatch.setenv("KNOWLEDGE_DB_POOL_MAX", "4")
    monkeypatch.setenv("KNOWLEDGE_DB_POOL_MIN", "10")
    kwargs = kdb._pool_kwargs()
    assert kwargs["min_size"] == 4 and kwargs["max_size"] == 4  # asyncpg would raise for min > max
    p = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], **kwargs)  # real startup path
    try:
        assert p.get_size() == 4
    finally:
        await p.close()
    # Idle close is disabled by default; with a tiny lifetime the same wiring does close idle connections.
    monkeypatch.setenv("KNOWLEDGE_DB_POOL_MIN", "3")
    assert kdb._pool_kwargs()["max_inactive_connection_lifetime"] == 0.0
    for idle, expected in (("0.3", 0), ("0", 3)):
        monkeypatch.setenv("KNOWLEDGE_DB_POOL_IDLE_S", idle)
        p = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], **kdb._pool_kwargs())
        try:
            await asyncio.sleep(1.0)
            assert p.get_size() == expected, (idle, p.get_size())
        finally:
            await p.close()


# === round 5 ===================================================================================================

async def test_a_stale_read_path_refill_cannot_overwrite_a_concurrent_writer(tenant_agent, monkeypatch):
    """Reader: Postgres says 'no KB'. Writer: attaches a KB and sets the flag. Reader's refill lands last."""
    from services.knowledge import agent_kb as agent_kb_service
    tenant, agent = tenant_agent
    t, a = tenant["slug"], agent["slug"]
    client = cache.get_client()
    key = cache._agent_kb_key(t, a)
    await client.delete(key)  # only this test's own key
    real_fill = cache.fill_has_enabled_kb

    async def writer_commits_then_reader_fills(tenant_slug, agent_slug, enabled):
        await cache.set_has_enabled_kb(tenant_slug, agent_slug, True)  # the writer's post-commit SET wins the race
        await real_fill(tenant_slug, agent_slug, enabled)  # the reader's stale value (False) arrives afterwards
    monkeypatch.setattr(cache, "fill_has_enabled_kb", writer_commits_then_reader_fills)
    try:
        set_caller_tenant(str(tenant["id"]))
        assert await agent_kb_service.has_enabled_kb(t, a) is False  # what the reader itself read
        assert await client.get(key) == "1"  # but the writer's value survived
        assert 0 < await client.ttl(key) <= 7 * 24 * 3600
    finally:
        await client.delete(key)


async def test_the_refill_is_nx_with_a_short_ttl_and_writers_overwrite():
    client = cache.get_client()
    key = cache._agent_kb_key(f"t{uuid.uuid4().hex[:6]}", "a")
    t, a = key.split(":")[2], "a"
    try:
        await cache.fill_has_enabled_kb(t, a, False)
        assert await client.get(key) == "0" and 0 < await client.ttl(key) <= 60
        await cache.fill_has_enabled_kb(t, a, True)
        assert await client.get(key) == "0"  # NX: an existing value is never replaced by a refill
        await cache.set_has_enabled_kb(t, a, True)
        assert await client.get(key) == "1"  # writers always overwrite
    finally:
        await client.delete(key)
