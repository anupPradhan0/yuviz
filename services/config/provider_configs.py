"""
Provider config CRUD — same cache-aside + audited-mutation pattern.

Returning api_key_ref to a caller is fine: it's a reference path
('k8s:voiceai/deepgram-api-key'), never a resolved secret — the AI Provider
Manager resolves it at provider-instantiation time via SecretResolver, not
here. audit.write_audit() still redacts it in the history trail as defense
in depth (see audit.py docstring).
"""

from __future__ import annotations

from typing import Any

import httpx

from . import audit, cache, db
from .secret_resolver import SecretResolver

_UPDATABLE_FIELDS = {
    "name", "engine", "model", "voice", "language", "region", "environment",
    "api_key_ref", "extra", "fallback_config_id",
}

# Conversation Service subscribes to this channel (see
# services/conversation/provider_config_subscriber.py) so an edit here
# evicts the corresponding cached provider client instantly, instead of
# needing a full process restart.
PROVIDER_CONFIG_CHANGED_CHANNEL = "provider_config_changed"


def _cache_key(provider_id: Any) -> str:
    return f"provider:{provider_id}"


async def get_provider_config(provider_id: Any) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(provider_id))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM provider_configs WHERE id = $1 AND deleted_at IS NULL", provider_id,
    )
    if row is None:
        return None

    result = dict(row)
    await cache.set_json(_cache_key(provider_id), result)
    return result


async def list_provider_configs(
    tenant_id: Any, *, role: str | None = None, environment: str | None = None,
) -> list[dict[str, Any]]:
    """Used to populate the Admin UI's STT/LLM/TTS dropdowns — prod-first,
    dev-last ordering is a presentation concern for the caller, not baked in
    here."""
    pool = await db.get_pool()
    conditions = ["tenant_id = $1", "deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if role is not None:
        params.append(role)
        conditions.append(f"role = ${len(params)}")
    if environment is not None:
        params.append(environment)
        conditions.append(f"environment = ${len(params)}")

    rows = await pool.fetch(
        f"SELECT * FROM provider_configs WHERE {' AND '.join(conditions)} ORDER BY name",
        *params,
    )
    return [dict(row) for row in rows]


async def create_provider_config(
    *,
    tenant_id: Any,
    name: str,
    role: str,
    engine: str,
    environment: str = "prod",
    model: str | None = None,
    voice: str | None = None,
    language: str | None = None,
    region: str | None = None,
    api_key_ref: str | None = None,
    extra: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    import json as _json

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO provider_configs "
                "(tenant_id, name, role, engine, environment, model, voice, language, region, api_key_ref, extra) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb) RETURNING *",
                tenant_id, name, role, engine, environment,
                model, voice, language, region, api_key_ref,
                _json.dumps(extra) if extra is not None else None,
            )
            result = dict(row)
            await audit.write_audit(
                conn,
                entity_type="provider_config",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=result,
            )
    return result


async def update_provider_config(
    provider_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_provider_config() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_provider_config() got non-updatable field(s): {unknown}")

    import json as _json
    if "extra" in fields and fields["extra"] is not None:
        fields = {**fields, "extra": _json.dumps(fields["extra"])}

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE — see tenants.py's update_tenant() comment: without
            # it, a concurrent update could make this transaction's audit
            # entry record a stale old_value.
            old_row = await conn.fetchrow(
                "SELECT * FROM provider_configs WHERE id = $1 FOR UPDATE", provider_id,
            )
            if old_row is None:
                raise LookupError(f"provider_config {provider_id} not found")
            old = dict(old_row)

            columns = list(fields.keys())
            set_parts = []
            for i, col in enumerate(columns):
                cast = "::jsonb" if col == "extra" else ""
                set_parts.append(f"{col} = ${i + 2}{cast}")
            new_row = await conn.fetchrow(
                f"UPDATE provider_configs SET {', '.join(set_parts)}, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                provider_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            # Scoped to the written columns, not the full row — otherwise
            # api_key_ref (redacted either way) rides along on every update
            # and the UI can't tell "redacted, unchanged" from "redacted,
            # changed."
            await audit.write_audit(
                conn,
                entity_type="provider_config",
                entity_id=provider_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value={col: old[col] for col in columns},
                new_value={col: new[col] for col in columns},
            )

    await cache.invalidate(_cache_key(provider_id))
    await cache.publish(PROVIDER_CONFIG_CHANGED_CHANNEL, str(provider_id))
    return new


async def soft_delete_provider_config(
    provider_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM provider_configs WHERE id = $1 FOR UPDATE", provider_id,
            )
            if old_row is None:
                raise LookupError(f"provider_config {provider_id} not found")
            old = dict(old_row)

            await conn.execute(
                "UPDATE provider_configs SET deleted_at = now() WHERE id = $1", provider_id,
            )
            await audit.write_audit(
                conn,
                entity_type="provider_config",
                entity_id=provider_id,
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
            )

    await cache.invalidate(_cache_key(provider_id))
    await cache.publish(PROVIDER_CONFIG_CHANGED_CHANNEL, str(provider_id))


_ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"


async def list_elevenlabs_voices(provider_id: Any, *, secret_resolver: SecretResolver) -> list[dict[str, Any]]:
    """Calls ElevenLabs' own Voices API server-side using provider_id's
    api_key_ref — the resolved key is used for this one outbound call and
    never returned to the caller (see secret_resolver.py's module
    docstring: this is the one place Config Service resolves a secret,
    specifically so the admin-ui never has to)."""
    cfg = await get_provider_config(provider_id)
    if cfg is None:
        raise LookupError(f"provider_config {provider_id} not found")
    if cfg["role"] != "tts" or cfg["engine"] != "elevenlabs":
        raise ValueError(
            f"provider_config {provider_id} is role={cfg['role']!r} engine={cfg['engine']!r}, "
            "expected role='tts' engine='elevenlabs'"
        )
    if not cfg["api_key_ref"]:
        raise ValueError(f"provider_config {provider_id} has no api_key_ref configured")

    api_key = await secret_resolver.resolve(cfg["api_key_ref"])
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_ELEVENLABS_VOICES_URL, headers={"xi-api-key": api_key})
    if resp.status_code != 200:
        raise ValueError(f"ElevenLabs Voices API returned {resp.status_code}: {resp.text[:200]}")

    return [
        {
            "voice_id": v["voice_id"],
            "name": v["name"],
            "category": v.get("category"),
            "labels": v.get("labels") or {},
            "preview_url": v.get("preview_url"),
        }
        for v in resp.json().get("voices", [])
    ]
