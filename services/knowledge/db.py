"""Process-wide Postgres pool for Knowledge Service (own lifecycle, shared database)."""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


def _pool_kwargs() -> dict:
    max_size = max(1, int(os.environ.get("KNOWLEDGE_DB_POOL_MAX", "20")))
    return {
        # min_size may never exceed max_size (a small KNOWLEDGE_DB_POOL_MAX must not crash startup).
        # Warm connections: retrieval acquires with a 60 ms bound, and opening a connection inside that bound under
        # a burst would read as overload.
        "min_size": min(int(os.environ.get("KNOWLEDGE_DB_POOL_MIN", "10")), max_size),
        "max_size": max_size,
        # asyncpg closes connections idle for 300 s no matter what min_size says, so after a quiet spell the pool
        # would be cold again. 0 disables the idle close (default). The cost is that a connection a firewall or
        # server dropped during a long idle is only found on first use; tcp_keepalives_idle below makes the server
        # probe idle sockets so such drops are rare and noticed early.
        "max_inactive_connection_lifetime": float(os.environ.get("KNOWLEDGE_DB_POOL_IDLE_S", "0")),
        # Startup parameters, not SET: asyncpg runs RESET ALL on every release, which would wipe a session SET.
        # The hnsw ones make the filtered HNSW scan keep going past ef_search (pgvector 0.8+); the vector
        # repository probes whether they took effect and compensates per query when they did not.
        "server_settings": {
            "hnsw.iterative_scan": "relaxed_order",
            "hnsw.max_scan_tuples": os.environ.get("KNOWLEDGE_HNSW_MAX_SCAN_TUPLES", "20000"),
            "tcp_keepalives_idle": "60",
        },
    }


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = dsn or os.environ["POSTGRES_DSN"]
        _pool = await asyncpg.create_pool(dsn, **_pool_kwargs())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
