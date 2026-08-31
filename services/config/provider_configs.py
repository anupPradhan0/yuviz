"""
Provider config CRUD — same cache-aside + audited-mutation pattern.

Returning api_key_ref to a caller is fine for the pointer schemes: it's a
reference path ('k8s:voiceai/deepgram-api-key'), never a resolved secret —
the AI Provider Manager resolves it at provider-instantiation time via
SecretResolver, not here. audit.write_audit() still redacts it in the
history trail as defense in depth (see audit.py docstring).

An `enc:` ref is different — it CARRIES the credential rather than pointing
at it (see libs/config_sdk/secrets.py). It is still returned on read, and
deliberately so: Conversation Service reads this endpoint on a Redis miss
and needs the sealed value to decrypt. What protects it is that Fernet
ciphertext is useless without SECRET_ENCRYPTION_KEY, which never leaves the
server. See the note above resolve_api_key_input() for when that should
become real server-side masking instead.
"""

from __future__ import annotations

from libs.config_sdk.secrets import ENCRYPTED_PREFIX, encrypt_secret, is_encrypted

# What the UI sees instead of a stored credential. Not the ciphertext
# either: there is no reason for a browser to hold it.
MASKED_SECRET = "__set__"

_REFERENCE_SCHEMES = ("env:", "k8s:", "vault:")


# ponytail: an `enc:` ref is returned as-is rather than masked server-side.
# Fernet ciphertext is worthless without SECRET_ENCRYPTION_KEY, which never
# leaves the server, so this is not a credential leak — and the alternative
# (masking here) would break the Conversation Service, which reads this very
# endpoint on a Redis miss and needs the sealed value to decrypt. The Admin
# UI renders any enc: ref as "key saved" and never shows the blob (see
# SecretRefInput.tsx). Mask server-side instead if the API ever gains a
# consumer that is neither a browser nor that service — which needs
# is_service_account on the JWT first.


def resolve_api_key_input(api_key: str | None, api_key_ref: str | None) -> str | None:
    """Turns whatever the UI sent into what belongs in the column.

    `api_key` is a credential the operator typed — encrypted here, before
    it can reach Postgres. `api_key_ref` is a pointer they typed, stored
    verbatim. A raw key pasted into the ref field is rejected rather than
    stored: that mistake put a live Gemini key in this column in plaintext
    (2026-08-28), and the only feedback was a stack trace in a log nobody
    was watching."""
    if api_key:
        return encrypt_secret(api_key.strip())
    if api_key_ref is None or api_key_ref == MASKED_SECRET:
        return None          # unchanged — the UI echoed the mask back
    ref = api_key_ref.strip()
    if not ref:
        return ""            # explicit clear
    if ref.startswith(_REFERENCE_SCHEMES) or ref.startswith(ENCRYPTED_PREFIX):
        return ref
    raise ValueError(
        "api_key_ref must point at a secret (env:VAR_NAME, vault:path#field or "
        "k8s:namespace/secret). To store the key itself, send it as `api_key` "
        "and it will be encrypted — never paste a real key into this field."
    )

import logging
from typing import Any

import httpx

from . import audit, cache, db
from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

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
    api_key: str | None = None,
    extra: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    import json as _json

    # A typed key is encrypted here, before it can reach Postgres.
    api_key_ref = resolve_api_key_input(api_key, api_key_ref)

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
    # `api_key` is a credential, not a column: it never appears in
    # _UPDATABLE_FIELDS, it is encrypted and folded into api_key_ref here.
    # Popping it before the unknown-field check is what keeps that true.
    typed_key = fields.pop("api_key", None)
    if typed_key or "api_key_ref" in fields:
        resolved = resolve_api_key_input(typed_key, fields.get("api_key_ref"))
        if resolved is None:
            fields.pop("api_key_ref", None)   # the UI echoed the mask back — leave it alone
        else:
            fields["api_key_ref"] = resolved

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
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_ELEVENLABS_VOICES_URL, headers={"xi-api-key": api_key})
    except httpx.RequestError as exc:
        # DNS failure, connect timeout, read timeout — nothing else catches
        # this (the router doesn't either), so left unhandled it reaches the
        # admin-ui as a bare 500 with no actionable detail.
        raise ValueError(f"could not reach the ElevenLabs Voices API: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        # Log the real response body (useful for debugging a bad key/rate
        # limit) but never forward it in the ValueError: the router maps
        # ValueError to a 400 `detail`, and the ElevenLabs response body is
        # not ours to hand back to the admin-ui caller verbatim.
        log.warning(
            "ElevenLabs Voices API returned %s for provider_config %s: %s",
            resp.status_code, provider_id, resp.text[:200],
        )
        raise ValueError(f"ElevenLabs Voices API returned {resp.status_code}")

    return [
        {
            "voice_id": v["voice_id"],
            "name": v["name"],
            "category": v.get("category"),
            "labels": v.get("labels") or {},
            "preview_url": v.get("preview_url"),
            # ElevenLabs documents `labels` as arbitrary, unvalidated
            # metadata (any string a voice's owner chose to tag it with) —
            # `verified_languages` is the actual validated field: each entry
            # is a real ISO 639-1 code this voice has been confirmed to
            # speak, with its own model_id/accent/locale/preview_url. Not
            # every voice has this populated (e.g. voices never run through
            # ElevenLabs' verification), so callers should fall back to
            # `labels` when it's empty, not assume it's always present.
            "verified_languages": v.get("verified_languages") or [],
        }
        for v in resp.json().get("voices", [])
    ]
