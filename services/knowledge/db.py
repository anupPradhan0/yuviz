"""Process-wide Postgres pool for Knowledge Service (own lifecycle, shared database)."""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = dsn or os.environ["POSTGRES_DSN"]
        # Startup parameters, not SET: asyncpg runs RESET ALL on every release, which would wipe a session SET.
        # They make the filtered HNSW scan keep going past ef_search (pgvector 0.8+); the vector repository
        # probes whether they took effect and compensates per query when they did not.
        _pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=int(os.environ.get("KNOWLEDGE_DB_POOL_MAX", "20")),
            server_settings={
                "hnsw.iterative_scan": "relaxed_order",
                "hnsw.max_scan_tuples": os.environ.get("KNOWLEDGE_HNSW_MAX_SCAN_TUPLES", "20000"),
            },
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
