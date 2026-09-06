"""
Postgres connection pool for Config Service.

One process-wide pool, lazily created on first use. Callers acquire a
connection per operation (`async with (await get_pool()).acquire() as conn`)
rather than holding one open — this module owns lifecycle only, not query
logic, which lives in tenants.py / agents.py / provider_configs.py etc.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None
log = logging.getLogger(__name__)


def json_col(value: Any) -> Any:
    """Decode a JSONB column. asyncpg returns strings (no pool codec — writers
    already pass json.dumps into $n::jsonb, so a codec would double-encode).

    Corrupt storage is a server defect — raise RuntimeError (→ 500), not ValueError
    (→ 400 with column bytes in the body).
    """
    if value is None or not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        log.exception("agents JSONB column is not decodable JSON")
        raise RuntimeError("agents JSONB column is not decodable JSON") from exc
    if isinstance(parsed, str):
        log.error("agents JSONB column is double-encoded JSON string")
        raise RuntimeError("agents JSONB column is double-encoded JSON string")
    return parsed


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
