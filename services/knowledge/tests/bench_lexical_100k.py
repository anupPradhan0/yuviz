"""Lexical-search cost on a 100k-chunk KB, before (every matching chunk ranked) and after (document-frequency
term selection). Run against a scratch database only:

    createdb scratch && psql scratch -f database/schema.sql -f database/knowledge_schema.sql ... (see the brief)
    POSTGRES_DSN=postgresql://.../scratch python services/knowledge/tests/bench_lexical_100k.py

Seeds Zipf-like text server-side if the database has no chunks yet (about 1 minute), and leaves the data so the
database can simply be dropped afterwards."""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import asyncpg  # noqa: E402

from services.knowledge import vector_repository as vr  # noqa: E402

TENANT, KB, DOC = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"
QUERIES = {
    "5 very common words": "w1 w2 w3 w5 w8",
    "12 very common words": "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10 w11 w12",
    "1 common + 2 mid words": "w1 w200 w900",
    "2 rarer words": "w700 w1100",
}


async def main() -> None:
    dsn = os.environ["POSTGRES_DSN"]
    if dsn.rstrip("/").endswith("/voiceai"):
        sys.exit("refusing to seed the shared dev database")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    if not await pool.fetchval("SELECT count(*) FROM kb_chunks"):
        await pool.execute(f"""
            INSERT INTO tenants (id, name, slug) VALUES ('{TENANT}', 'Big', 'big');
            INSERT INTO knowledge_bases (id, tenant_id, slug, name) VALUES ('{KB}', '{TENANT}', 'kb', 'kb');
            INSERT INTO kb_documents (id, kb_id, tenant_id, title, source_ref, content_type, status)
              VALUES ('{DOC}', '{KB}', '{TENANT}', 'D', 'x', 'text/plain', 'ready');
            INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count)
              SELECT '{DOC}', '{KB}', '{TENANT}', g,
                     (SELECT string_agg('w' || floor(power(1500, random()))::int, ' ')
                        FROM generate_series(1, 60) WHERE g IS NOT NULL), 60
                FROM generate_series(1, 100000) g;
            ANALYZE kb_chunks;""")
    n = await pool.fetchval("SELECT count(*) FROM kb_chunks")
    print(f"{n} chunks; machine load matters, N=7 per cell")
    print(f"{'query':26} {'before p50/max ms':>20} {'after first (probe) ms':>24} {'after warm p50/max ms':>24}  rows")
    for name, q in QUERIES.items():
        before = []
        for _ in range(7):
            async with pool.acquire() as c, c.transaction():
                t = time.perf_counter()
                await c.fetch(vr._LEXICAL_SQL, vr.build_tsquery(q), [KB], 15, vr.SEARCHABLE_STATUSES, 0.05)
                before.append((time.perf_counter() - t) * 1000)
        repo = vr.PgVectorRepository()
        after = []
        for _ in range(8):
            async with pool.acquire() as c, c.transaction():
                t = time.perf_counter()
                rows = await repo.lexical_search(c, [KB], q, 15, 0.05, approx_chunks=n)
                after.append((time.perf_counter() - t) * 1000)
        warm = after[1:]
        print(f"{name:26} {statistics.median(before):9.1f}/{max(before):9.1f} {after[0]:24.1f} "
              f"{statistics.median(warm):13.1f}/{max(warm):9.1f}  {len(rows)}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
