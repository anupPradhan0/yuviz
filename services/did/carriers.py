"""
Read-only carriers accessor for DID Service — this service only ever reads
a carrier's own credentials to authenticate its provider API calls; it
never writes to carriers (services/config/carriers.py owns that table's
CRUD). Duplicated as its own thin module rather than cross-imported, same
microservice-boundary reasoning as db.py/secret_resolver.py/audit.py.
"""

from __future__ import annotations

from typing import Any

from . import db


async def get_carrier_by_id(carrier_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM carriers WHERE id = $1 AND deleted_at IS NULL", carrier_id,
    )
    return dict(row) if row is not None else None
