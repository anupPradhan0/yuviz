"""
Postgres connection pool for DID Service — its own process-wide pool, not a
shared import from services.config.db. Same microservice-boundary
reasoning as services/knowledge/db.py: both services point at the same
physical Postgres database (voiceai), but each service owns its own
connection lifecycle.
"""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = dsn or os.environ["POSTGRES_DSN"]
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
