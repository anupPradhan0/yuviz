"""
audit_log writer — always called inside the same transaction as the mutation
it's recording, so a config change and its audit entry commit or roll back
together (never a write that "succeeded" with no trace, or an orphaned audit
row for a write that failed).

Redacts known secret-reference fields before the value ever reaches
Postgres. This is defense in depth: api_key_ref/auth_token_ref are already
just reference paths, not resolved secrets — but redacting them in the audit
trail means a leaked audit_log row never reveals which Vault/K8s path to
target next, on top of never containing a live key. password_hash is
redacted too — a bcrypt hash isn't the plaintext password, but there's no
reason for it to sit in a JSONB audit column either.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import asyncpg

_SECRET_REF_FIELDS = {"api_key_ref", "auth_token_ref", "password_hash"}


def _redact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {k: ("[redacted]" if k in _SECRET_REF_FIELDS else v) for k, v in value.items()}


async def write_audit(
    conn: asyncpg.Connection,
    *,
    entity_type: str,
    entity_id: Any,
    action: Literal["created", "updated", "deleted"],
    user_id: Any | None = None,
    user_email: str | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log "
        "(entity_type, entity_id, user_id, user_email, action, old_value, new_value, ip_address) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
        entity_type,
        entity_id,
        user_id,
        user_email,
        action,
        json.dumps(_redact(old_value), default=str) if old_value is not None else None,
        json.dumps(_redact(new_value), default=str) if new_value is not None else None,
        ip_address,
    )
