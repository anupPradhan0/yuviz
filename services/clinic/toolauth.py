"""Authenticates toolexec's calls to the clinic tool API.

Each tenant has its own key; toolexec sends it as a bearer token from the custom_api row's
enc: ref. The tenant comes from the key and nowhere else, so a tool call can never read or
change another tenant's clinic, whatever arguments the model supplies.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Header, HTTPException

from libs.tenancy import platform_conn

from . import db

KEY_PREFIX = "clk_"


def generate_tool_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_tool_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def require_tool_tenant(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: the tenant id the bearer key belongs to, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer key")
    key = authorization[7:].strip()
    if not key.startswith(KEY_PREFIX):
        raise HTTPException(status_code=401, detail="invalid key")
    pool = await db.get_pool()
    # The one lookup that cannot be tenant-scoped: the key is what names the tenant.
    async with platform_conn(pool, reason="clinic-tool-key-lookup") as conn:
        tenant_id = await conn.fetchval(
            "SELECT tenant_id FROM clinic_api_keys WHERE key_hash = $1 AND revoked_at IS NULL",
            hash_tool_key(key),
        )
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="invalid key")
    return str(tenant_id)
