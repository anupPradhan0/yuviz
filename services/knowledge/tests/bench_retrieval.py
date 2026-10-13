"""Retrieval latency benchmark: seeded KB, deterministic fake embedder with injected latency, real Postgres.

    POSTGRES_DSN=postgresql://.../scratchdb python services/knowledge/tests/bench_retrieval.py [--n 300] [--chunks 4000]

Run it against a scratch database only: it seeds a tenant, agent and KB, and deletes them again on exit.
Scenarios are sequential single-caller runs (plus one 8-way concurrent run), so the numbers are latency of one
retrieve() call on an otherwise idle machine, not throughput. The database is on localhost: network round trips are
~0, so absolute numbers understate a remote database; the fault-injection scenarios measure the deadline, which
does not depend on that."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import random
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "..")))
os.environ.setdefault("JWT_SECRET", "bench-only-" * 8)

import asyncpg  # noqa: E402
from rag_fakes import FakeEmbedder, embed_text, make_manager, vec_literal  # noqa: E402

from libs.tenancy import set_caller_tenant  # noqa: E402
from services.config import provider_configs  # noqa: E402
from services.knowledge import agent_kb, db, retrieval, retrieval_log  # noqa: E402
from services.knowledge import retrieval_plan as plans  # noqa: E402
from services.knowledge import knowledge_bases as kb_service  # noqa: E402
from services.knowledge.resilience import RetrievalConfig, RetrievalUnavailable  # noqa: E402
from services.knowledge.vector_repository import PgVectorRepository  # noqa: E402

VOCAB = [f"term{i}" for i in range(500)]


class FaultRepo(PgVectorRepository):
    def __init__(self, dense_sleep=0.0, lexical_sleep=0.0, lexical_fail=False):
        super().__init__()
        self.dense_sleep, self.lexical_sleep, self.lexical_fail = dense_sleep, lexical_sleep, lexical_fail

    async def search(self, conn, *a, **kw):
        if self.dense_sleep:
            await conn.execute("SELECT pg_sleep($1)", self.dense_sleep)  # a genuinely slow statement
        return await super().search(conn, *a, **kw)

    async def lexical_search(self, conn, *a, **kw):
        if self.lexical_sleep:
            await conn.execute("SELECT pg_sleep($1)", self.lexical_sleep)
        if self.lexical_fail:
            raise asyncpg.PostgresError("injected lexical failure")
        return await super().lexical_search(conn, *a, **kw)


class DeadReranker:
    configured = True

    async def rerank(self, query, matches):
        raise RuntimeError("reranker down")


async def seed(pool, n_chunks: int):
    rng = random.Random(7)
    slug = f"bench-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'bench', 'Bench') RETURNING *", tenant["id"]))
    set_caller_tenant(str(tenant["id"]))
    cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="E", role="embedding", engine="ollama", allow_pointer_schemes=False)
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="kb", name="kb", embedding_config_id=cfg["id"])
    await agent_kb.assign(agent["id"], kb["id"])
    docs = max(1, n_chunks // 20)
    for d in range(docs):
        doc = await pool.fetchrow(
            "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status) "
            "VALUES ($1, $2, $3, 'x', 'text/plain', 'ready') RETURNING id", kb["id"], tenant["id"], f"Doc {d}")
        rows = []
        idx = 0
        for sec in range(5):  # 5 sections per doc, 4 children each
            children = [" ".join(rng.choice(VOCAB) for _ in range(40)) for _ in range(4)]
            parent_id = await pool.fetchval(
                "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count, is_parent) "
                "VALUES ($1, $2, $3, $4, $5, $6, true) RETURNING id",
                doc["id"], kb["id"], tenant["id"], -1 - sec, " ".join(children), 160)
            for text in children:
                rows.append((doc["id"], kb["id"], tenant["id"], idx, text, 40, vec_literal(embed_text(text)), parent_id))
                idx += 1
        await pool.executemany(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count, embedding, "
            "parent_id) VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8)", rows)
    await pool.execute("ANALYZE kb_chunks")
    return tenant, agent, kb, cfg


async def cleanup(pool, tenant, agent, kb, cfg):
    await retrieval_log.flush()
    await pool.execute("DELETE FROM retrieval_logs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def query(rng, k=6):
    return " ".join(rng.choice(VOCAB) for _ in range(k))


async def scenario(name, n, make_call, results, *, concurrency=1):
    lat, statuses = [], {}
    sem = asyncio.Semaphore(concurrency)

    async def one(i):
        async with sem:
            t0 = time.perf_counter()
            try:
                status = (await make_call(i))["status"]
            except RetrievalUnavailable:
                status = "unavailable(raised)"
            lat.append((time.perf_counter() - t0) * 1000)
            statuses[status] = statuses.get(status, 0) + 1

    await asyncio.gather(*[one(i) for i in range(n)])
    await retrieval_log.flush(timeout=30)
    results.append((name, n, pct(lat, 50), pct(lat, 95), pct(lat, 99), max(lat), statuses))


async def main(n: int, n_chunks: int) -> None:
    pool = await db.get_pool()
    tenant, agent, kb, cfg = await seed(pool, n_chunks)
    count = await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE tenant_id = $1 AND NOT is_parent", tenant["id"])
    print(f"seeded {count} child chunks (+{count // 4} parents), 1 KB, tenant {tenant['slug']}")
    rng = random.Random(11)
    results: list = []
    scope = {"tenant_slug": tenant["slug"], "agent_slug": agent["slug"]}

    def call(embedder, repo=None, **kw):
        manager = make_manager(embedder)
        repo = repo or PgVectorRepository()

        async def fn(i, q=None):
            set_caller_tenant(str(tenant["id"]))
            return await retrieval.retrieve(None, repo, manager, query=q or query(rng), **scope, **kw)
        return fn

    try:
        # Warm everything once: plan, pool connections, capability probe.
        shared_repo = PgVectorRepository()
        await call(FakeEmbedder(), shared_repo)(0)

        hit_queries = [query(rng) for _ in range(50)]
        hit_embedder = FakeEmbedder()
        hit_call = call(hit_embedder, shared_repo)
        for q in hit_queries:  # prime the embedding cache
            await hit_call(0, q)
        await scenario("A. warm plan + embed cache HIT", n, lambda i: hit_call(i, hit_queries[i % 50]), results)
        await scenario("A8. same, 8 concurrent callers", n, lambda i: hit_call(i, hit_queries[i % 50]), results,
                       concurrency=8)

        miss_call = call(FakeEmbedder(latency_s=0.060), shared_repo)
        await scenario("B. warm plan + embed MISS (60 ms injected)", n, miss_call, results)

        def cold(i):
            plans.clear_cache()  # the plan only: the embedding cache stays warm, so this row is "cold plan, embed hit"
            return hit_call(i, hit_queries[i % 50])
        await scenario("D. cold plan every call (embed hit)", min(n, 100), cold, results)

        hang = call(FakeEmbedder(latency_s=5.0), shared_repo)
        await scenario("C1. embedder hangs 5 s (breaker opens after 5)", n, hang, results)

        # From here the breaker is switched off, so the numbers are the worst case: timeouts and the deadline alone.
        no_breaker = RetrievalConfig(breaker_failures=10**9)
        retrieval.clear_caches()
        await call(FakeEmbedder(), shared_repo, config=no_breaker)(0)
        await scenario("C1b. embedder hangs 5 s, breaker disabled", n,
                       call(FakeEmbedder(latency_s=5.0), shared_repo, config=no_breaker), results)
        await scenario("C2. DB slowed 500 ms (pg_sleep) on both branches", n,
                       call(FakeEmbedder(latency_s=0.060), FaultRepo(dense_sleep=0.5, lexical_sleep=0.5),
                            config=no_breaker), results)
        await scenario("C3. lexical branch errors (dense answers)", n,
                       call(FakeEmbedder(latency_s=0.060), FaultRepo(lexical_fail=True), config=no_breaker), results)
        await scenario("C4. dense DB statement slowed 500 ms (lexical answers)", n,
                       call(FakeEmbedder(latency_s=0.060), FaultRepo(dense_sleep=0.5), config=no_breaker), results)
        await scenario("C5. reranker dead, rerank=true", n,
                       call(FakeEmbedder(latency_s=0.060), shared_repo, rerank=True, reranker=DeadReranker(),
                            config=no_breaker), results)
        await scenario("C6. embedder hung AND DB slowed (typed unavailable)", n,
                       call(FakeEmbedder(latency_s=5.0), FaultRepo(dense_sleep=0.5, lexical_sleep=0.5),
                            config=no_breaker), results)
    finally:
        await cleanup(pool, tenant, agent, kb, cfg)
        await db.close_pool()

    print(f"\nmachine: {platform.platform()} | {platform.processor() or platform.machine()} | python {platform.python_version()}"
          f" | deadline {RetrievalConfig.from_env().deadline_s * 1000:.0f} ms | N={n} per scenario")
    print(f"{'scenario':58} {'n':>4} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}  statuses (ms)")
    for name, cnt, p50, p95, p99, mx, statuses in results:
        print(f"{name:58} {cnt:>4} {p50:7.1f} {p95:7.1f} {p99:7.1f} {mx:7.1f}  {statuses}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--chunks", type=int, default=4000)
    args = ap.parse_args()
    if os.environ.get("POSTGRES_DSN", "").rstrip("/").endswith("/voiceai"):
        sys.exit("refusing to seed the shared dev database; point POSTGRES_DSN at a scratch database")
    asyncio.run(main(args.n, args.chunks))
