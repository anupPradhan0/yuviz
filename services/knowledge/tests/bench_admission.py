"""Admission/contention benchmark: 40 concurrent retrievals (conn=None and the router's connection-holding shape,
one tenant and eight) and the noisy-neighbour case. Scratch database only.

    POSTGRES_DSN=postgresql://.../scratchdb python services/knowledge/tests/bench_admission.py
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "..")))
os.environ.setdefault("JWT_SECRET", "bench-only-" * 8)

from rag_fakes import FakeEmbedder, add_document, make_kb, make_manager  # noqa: E402

from libs.tenancy import set_caller_tenant, tenant_conn  # noqa: E402
from services.knowledge import db, retrieval, retrieval_log  # noqa: E402
from services.knowledge.resilience import RetrievalConfig, RetrievalUnavailable  # noqa: E402
from services.knowledge.vector_repository import PgVectorRepository  # noqa: E402

DEGRADED: dict[str, int] = {}  # degraded tags seen in the scenario (why 'partial' happened)
VOCAB = [f"term{i}" for i in range(300)]


class SlowRepo(PgVectorRepository):
    """+sleep_s of real DB time (pg_sleep) per search statement."""

    def __init__(self, sleep_s: float, dense_only: bool = False):
        super().__init__()
        self.sleep_s, self.dense_only = sleep_s, dense_only

    async def search(self, conn, *a, **kw):
        await conn.execute("SELECT pg_sleep($1)", self.sleep_s)
        return await super().search(conn, *a, **kw)

    async def lexical_search(self, conn, *a, **kw):
        if not self.dense_only:
            await conn.execute("SELECT pg_sleep($1)", self.sleep_s)
        return await super().lexical_search(conn, *a, **kw)


async def seed_tenant(pool, rng, n_chunks=200):
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'bench', 'B') RETURNING *", tenant["id"]))
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "kb")
    await add_document(pool, tenant, kb, "Doc", [" ".join(rng.choice(VOCAB) for _ in range(20)) for _ in range(n_chunks)])
    return tenant, agent


async def cleanup(pool, tenants):
    await retrieval_log.flush(timeout=30)
    for t, a in tenants:
        for sql in ("DELETE FROM retrieval_logs WHERE tenant_id = $1", "DELETE FROM agent_knowledge_bases WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
                    "DELETE FROM kb_chunks WHERE tenant_id = $1", "DELETE FROM kb_documents WHERE tenant_id = $1",
                    "DELETE FROM knowledge_bases WHERE tenant_id = $1", "DELETE FROM agents WHERE tenant_id = $1",
                    "DELETE FROM provider_configs WHERE tenant_id = $1", "DELETE FROM tenants WHERE id = $1"):
            await pool.execute(sql, t["id"])


async def warm(t, a):
    set_caller_tenant(str(t["id"]))
    await retrieval.retrieve(None, PgVectorRepository(), make_manager(FakeEmbedder()), tenant_slug=t["slug"],
                             agent_slug=a["slug"], query="term1 term2")


async def one(pool, tenant, agent, repo, embedder, query, held_shape, results, name=None, cfg=None):
    set_caller_tenant(str(tenant["id"]))
    t0 = time.perf_counter()
    try:
        kw = dict(tenant_slug=tenant["slug"], agent_slug=agent["slug"], query=query)
        if cfg is not None:
            kw["config"] = cfg
        if held_shape:
            async with tenant_conn(pool) as held:
                r = await retrieval.retrieve(held, repo, make_manager(embedder), **kw)
        else:
            r = await retrieval.retrieve(None, repo, make_manager(embedder), **kw)
        status = r["status"]
        for tag in r["degraded"]:
            DEGRADED[tag] = DEGRADED.get(tag, 0) + 1
    except RetrievalUnavailable as exc:
        status = "unavailable(overloaded)" if "overloaded" in exc.degraded else "unavailable"
    results.append((name or tenant["slug"], status, (time.perf_counter() - t0) * 1000))


async def watch_pool(pool, peak):
    while True:
        peak[0] = max(peak[0], pool.get_size() - pool.get_idle_size())
        await asyncio.sleep(0.002)


def split(results):
    out = {}
    for _, status, _ in results:
        out[status] = out.get(status, 0) + 1
    return out


async def main():
    pool = await db.get_pool()
    rng = random.Random(3)
    tenants = [await seed_tenant(pool, rng) for _ in range(8)]
    cfg = RetrievalConfig.from_env()
    print(f"caps: global {cfg.max_inflight}, per tenant {cfg.tenant_max_inflight}, queue wait {cfg.queue_wait_s*1000:.0f} ms, "
          f"pool max {pool.get_max_size()}")
    try:
        for t, a in tenants:  # warm plans
            await warm(t, a)
        print(f"\n{'scenario':58} {'ok':>4} {'partial':>8} {'unavail':>8} {'overload':>9} {'p50':>7} {'max':>7} {'pool peak':>10}")
        for label, held_shape, n_tenants in (("40 callers, conn=None, 1 tenant", False, 1), ("40 callers, conn=None, 8 tenants", False, 8),
                                             ("40 callers, router holds a conn, 1 tenant", True, 1),
                                             ("40 callers, router holds a conn, 8 tenants", True, 8)):
            retrieval.clear_caches()
            for t, a in tenants[:n_tenants]:
                await warm(t, a)
            DEGRADED.clear()
            repo, embedder, results, peak = SlowRepo(0.010), FakeEmbedder(latency_s=0.060), [], [0]
            watcher = asyncio.create_task(watch_pool(pool, peak))
            jobs = []
            for i in range(40):
                t, a = tenants[i % n_tenants]
                jobs.append(one(pool, t, a, repo, embedder, " ".join(rng.choice(VOCAB) for _ in range(5)) + f" q{i}", held_shape, results))
            await asyncio.gather(*jobs)
            watcher.cancel()
            sp, lat = split(results), [r[2] for r in results]
            print(f"{label:58} {sp.get('ok', 0):>4} {sp.get('partial', 0):>8} {sp.get('unavailable', 0):>8} "
                  f"{sp.get('unavailable(overloaded)', 0):>9} {statistics.median(lat):7.1f} {max(lat):7.1f} {peak[0]:>10}"
                  f"  {({k: v for k, v in DEGRADED.items() if k != 'plan_cache_miss'}) or ''}")

        # Noisy neighbour: tenant 0 sends 20 slow requests (150 ms of DB time each), 7 others send one normal request.
        retrieval.clear_caches()
        noisy, others = tenants[0], tenants[1:]
        for t, a in tenants:
            await warm(t, a)
        DEGRADED.clear()
        results, jobs = [], []
        slow, fast = SlowRepo(0.150, dense_only=True), PgVectorRepository()
        for i in range(20):
            jobs.append(one(pool, noisy[0], noisy[1], slow, FakeEmbedder(), f"term{i} slow{i}", False, results, name="noisy"))
        for i, (t, a) in enumerate(others):
            jobs.append(one(pool, t, a, fast, FakeEmbedder(), f"term{i} quiet", False, results, name="other"))
        await asyncio.gather(*jobs)
        for who in ("other", "noisy"):
            rows = [r for r in results if r[0] == who]
            lat = [r[2] for r in rows]
            print(f"\nnoisy-neighbour, {who:5}: n={len(rows)} p50={statistics.median(lat):.1f} ms max={max(lat):.1f} ms outcomes={split(rows)}")
    finally:
        await cleanup(pool, tenants)
        await db.close_pool()


if __name__ == "__main__":
    if os.environ.get("POSTGRES_DSN", "").rstrip("/").endswith("/voiceai"):
        sys.exit("refusing to run against the shared dev database")
    asyncio.run(main())
