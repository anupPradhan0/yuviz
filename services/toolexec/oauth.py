"""
Per-tenant OAuth2 authorization-code connections: the provider registry, the
authorize/redeem flow, token access with refresh, and disconnect.

One oauth_connections row per (tenant, provider), mutated in place. Every
statement carries `tenant_id = $1` (lesson 36); tenant_conn's RLS is the
second layer. Every ref this module writes is a tenant-bound `enc:t1.`
ciphertext and is read back only through auth_schemes.resolve_tenant_ref.

Provider calls go through resolve_and_validate_endpoint, a pinned transport
and a 10s timeout. Secrets travel only in a form body (`data=`), never in the
URL: httpx logs full URLs. Failures are re-raised as bare ValueErrors with a
fixed code so no provider response body or request field reaches a log line.

`client_id`/`client_secret` are platform values from env, resolved by the
platform CompositeSecretResolver, never the tenant resolver.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.background import BackgroundTasks

from libs.config_sdk.secret_resolver import CompositeSecretResolver
from libs.config_sdk.secrets import encrypt_tenant_secret
from libs.tenancy import platform_conn, tenant_conn

from . import audit, auth_schemes, db
from .auth_schemes import ReconnectRequired
from .custom_apis import PinnedResolverTransport, resolve_and_validate_endpoint

log = logging.getLogger(__name__)

_platform_secret_resolver = CompositeSecretResolver()  # the PLATFORM resolver — never the tenant-namespaced one

_STATE_TTL_MINUTES = 10
_EXPIRY_SKEW_SECONDS = 60
_PROVIDER_TIMEOUT_S = 10.0
_REDIRECT_URI_ENV = "TOOLEXEC_OAUTH_REDIRECT_URI"

# What a connection row may reveal: no *_ref, no provider_sub.
_PUBLIC_COLUMNS = "id, provider, status, account_label, scopes, updated_at"


@dataclass(frozen=True)
class OAuthProvider:
    key: str
    label: str
    authorize_url: str                # browser-dialed only; never fetched by the backend
    token_url: str                    # Zoho: formatted from the allow-listed accounts_server
    revoke_url: str | None            # Microsoft: None (no token-revoke endpoint)
    identity_scopes: frozenset[str]
    api_hosts: frozenset[str]         # the ONLY hosts this provider's token may be sent to
    accounts_servers: frozenset[str] = frozenset()   # Zoho DC origins
    userinfo_url: str | None = None   # set when identity is not in the id_token (Zoho)
    scope_separator: str = " "
    extra_authorize_params: tuple[tuple[str, str], ...] = ()


_ZOHO_TLDS = ("com", "eu", "in", "com.au", "jp", "ca", "sa", "uk")

PROVIDERS: dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        key="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        identity_scopes=frozenset({"openid", "email"}),
        api_hosts=frozenset({"www.googleapis.com", "sheets.googleapis.com"}),
        extra_authorize_params=(
            ("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true"),
        ),
    ),
    "microsoft": OAuthProvider(
        key="microsoft",
        label="Microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        revoke_url=None,
        identity_scopes=frozenset({"openid", "email", "offline_access"}),
        api_hosts=frozenset({"graph.microsoft.com"}),
        extra_authorize_params=(("prompt", "consent"),),
    ),
    "zoho": OAuthProvider(
        key="zoho",
        label="Zoho",
        authorize_url="https://accounts.zoho.com/oauth/v2/auth",
        token_url="{accounts_server}/oauth/v2/token",
        revoke_url="{accounts_server}/oauth/v2/token/revoke",
        identity_scopes=frozenset({"AaaServer.profile.Read"}),
        api_hosts=frozenset(f"www.zohoapis.{tld}" for tld in _ZOHO_TLDS),
        accounts_servers=frozenset(f"https://accounts.zoho.{tld}" for tld in _ZOHO_TLDS),
        userinfo_url="{accounts_server}/oauth/user/info",
        scope_separator=",",
        extra_authorize_params=(("access_type", "offline"), ("prompt", "consent")),
    ),
}

def _client_id(provider: OAuthProvider) -> str | None:
    return os.environ.get(f"TOOLEXEC_OAUTH_{provider.key.upper()}_CLIENT_ID")


def _client_secret_ref(provider: OAuthProvider) -> str | None:
    return os.environ.get(f"TOOLEXEC_OAUTH_{provider.key.upper()}_CLIENT_SECRET_REF")


def configured_providers() -> list[str]:
    """A provider with any of its env unset is hidden, so a tenant with no
    platform configuration sees no connector and can reach nothing."""
    if not os.environ.get(_REDIRECT_URI_ENV):
        return []
    return [key for key, p in PROVIDERS.items() if _client_id(p) and _client_secret_ref(p)]


async def _client_credentials(provider: OAuthProvider) -> tuple[str, str]:
    return _client_id(provider), await _platform_secret_resolver.resolve(_client_secret_ref(provider))


def _provider_transport(allowed_ips: list[str]) -> httpx.AsyncHTTPTransport:
    """Isolated call site so tests can monkeypatch this to inject an
    httpx.MockTransport — same pattern as executor._step_transport."""
    return PinnedResolverTransport(allowed_ips)


async def _provider_call(
    method: str, url: str, *, data: dict[str, str] | None = None, json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """The only outbound path to a provider. `data` is a form body and
    `json_body` a JSON one; there is deliberately no `params=`, so a secret
    cannot reach the URL."""
    _hostname, allowed_ips = await resolve_and_validate_endpoint(url)
    async with httpx.AsyncClient(transport=_provider_transport(allowed_ips), timeout=_PROVIDER_TIMEOUT_S) as client:
        return await client.request(method, url, data=data, json=json_body, headers=headers)


def _expires_at(body: dict[str, Any]) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(body.get("expires_in", 3600)))


def _id_token_claims(id_token: str) -> dict[str, Any]:
    # Straight from the provider's token endpoint over TLS (OIDC Core
    # 3.1.3.7), and used for display and the shared-grant check only.
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def start_authorization(*, tenant_id: str, user_id: str, provider: str, preset_key: str | None) -> str:
    if provider not in configured_providers():
        raise ValueError("oauth_provider_unavailable")
    spec = PROVIDERS[provider]
    preset_scopes = frozenset()
    if preset_key is not None:
        from . import presets  # function-local: presets imports this module

        # Scopes come only from the preset definitions, never from tenant input.
        preset = presets.PRESETS.get(preset_key)
        if preset is None or preset.provider != provider:
            raise ValueError("unknown_preset")
        preset_scopes = preset.scopes

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM oauth_authorization_states WHERE tenant_id = $1 AND expires_at < now()", tenant_id,
        )
        existing = await conn.fetchval(
            "SELECT scopes FROM oauth_connections WHERE tenant_id = $1 AND provider = $2 AND deleted_at IS NULL",
            tenant_id, provider,
        )
        scopes = sorted(spec.identity_scopes | preset_scopes | set(existing or ()))
        await conn.execute(
            "INSERT INTO oauth_authorization_states "
            "(tenant_id, user_id, provider, state_hash, code_verifier_ref, scopes, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(mins => $7))",
            tenant_id, user_id, provider, hashlib.sha256(state.encode()).hexdigest(),
            encrypt_tenant_secret(tenant_id, verifier), scopes, _STATE_TTL_MINUTES,
        )

    query = httpx.QueryParams({
        "response_type": "code",
        "client_id": _client_id(spec),
        "redirect_uri": os.environ[_REDIRECT_URI_ENV],
        "scope": spec.scope_separator.join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(spec.extra_authorize_params),
    })
    return f"{spec.authorize_url}?{query}"


async def complete_authorization(
    *, tenant_id: str, user_id: str, user_email: str | None, state: str, code: str, accounts_server: str | None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        redeemed = await conn.fetchrow(
            "UPDATE oauth_authorization_states SET consumed_at = now() "
            " WHERE tenant_id = $1 AND state_hash = $2 AND user_id = $3 "
            "   AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING provider, code_verifier_ref, scopes",
            tenant_id, hashlib.sha256(state.encode()).hexdigest(), user_id,
        )
    if redeemed is None:
        raise ValueError("oauth_connection_failed")

    # The exchange runs after the redeem commits: no transaction is held
    # across network I/O, and a consumed state that failed here stays spent.
    try:
        spec = PROVIDERS[redeemed["provider"]]
        if redeemed["provider"] not in configured_providers():
            raise ValueError
        if spec.accounts_servers:
            if accounts_server not in spec.accounts_servers:
                raise ValueError
        else:
            accounts_server = None
        verifier = await auth_schemes.resolve_tenant_ref(tenant_id, redeemed["code_verifier_ref"])
        client_id, client_secret = await _client_credentials(spec)
        resp = await _provider_call("POST", spec.token_url.format(accounts_server=accounts_server), data={
            "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
            "code": code, "code_verifier": verifier, "redirect_uri": os.environ[_REDIRECT_URI_ENV],
        })
        resp.raise_for_status()
        body = resp.json()
        access_token, refresh_token = body["access_token"], body["refresh_token"]
        if spec.userinfo_url is None:
            claims = _id_token_claims(body["id_token"]) if body.get("id_token") else {}
            account_label, provider_sub = claims.get("email"), claims.get("sub")
        else:
            info = await _provider_call(
                "GET", spec.userinfo_url.format(accounts_server=accounts_server),
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            info.raise_for_status()
            account_label, provider_sub = info.json().get("Email"), info.json().get("ZUID")
        access_ref = encrypt_tenant_secret(tenant_id, access_token)
        refresh_ref = encrypt_tenant_secret(tenant_id, refresh_token)
        access_expires_at = _expires_at(body)
    except Exception:
        raise ValueError("oauth_connection_failed") from None

    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO oauth_connections (tenant_id, provider, status, account_label, scopes, accounts_server, "
            "                               access_token_ref, access_expires_at, refresh_token_ref, connected_by, "
            "                               provider_sub) "
            "VALUES ($1, $2, 'connected', $3, $4, $5, $6, $7, $8, $9, $10) "
            "ON CONFLICT (tenant_id, provider) WHERE deleted_at IS NULL DO UPDATE SET "
            "  status = 'connected', account_label = EXCLUDED.account_label, scopes = EXCLUDED.scopes, "
            "  provider_sub = EXCLUDED.provider_sub, accounts_server = EXCLUDED.accounts_server, "
            "  access_token_ref = EXCLUDED.access_token_ref, access_expires_at = EXCLUDED.access_expires_at, "
            "  refresh_token_ref = EXCLUDED.refresh_token_ref, connected_by = EXCLUDED.connected_by, "
            "  updated_at = now() "
            f"RETURNING {_PUBLIC_COLUMNS}, (xmax = 0) AS inserted",  # xmax = 0 only on a fresh insert
            tenant_id, redeemed["provider"], account_label, redeemed["scopes"], accounts_server,
            access_ref, access_expires_at, refresh_ref, user_id, provider_sub,
        )
        connection = dict(row)
        inserted = connection.pop("inserted")
        await audit.write_audit(
            conn, entity_type="oauth_connection", entity_id=connection["id"],
            action="created" if inserted else "updated", user_id=user_id, user_email=user_email,
            new_value={"provider": connection["provider"], "status": connection["status"],
                       "scopes": connection["scopes"]},
        )
    return connection


async def list_connections(tenant_id: str) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            f"SELECT {_PUBLIC_COLUMNS} FROM oauth_connections "
            "WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY provider",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def get_connected(conn, tenant_id: str, provider: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT id, scopes FROM oauth_connections "
        "WHERE tenant_id = $1 AND provider = $2 AND deleted_at IS NULL AND status = 'connected'",
        tenant_id, provider,
    )
    return dict(row) if row is not None else None


async def post_json(tenant_id: str, connection_id: Any, url: str, body: Any) -> Any:
    """One authenticated JSON POST to a connected provider's API, for a preset's
    setup calls (creating the leads sheet). The token goes only in the
    Authorization header and only to one of the provider's own API hosts."""
    token, provider = await access_token_for(tenant_id, connection_id)
    if urlsplit(url).hostname not in provider.api_hosts:
        raise ValueError("credential_unavailable")
    resp = await _provider_call("POST", url, json_body=body, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


async def access_token_for(tenant_id: str, connection_id: str) -> tuple[str, OAuthProvider]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM oauth_connections WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL",
            tenant_id, connection_id,
        )
    if row is None or row["status"] != "connected":
        raise ReconnectRequired
    spec = PROVIDERS[row["provider"]]
    remaining = row["access_expires_at"] - datetime.datetime.now(datetime.timezone.utc)
    if remaining.total_seconds() > _EXPIRY_SKEW_SECONDS:
        return await auth_schemes.resolve_tenant_ref(tenant_id, row["access_token_ref"]), spec
    return await _refresh(tenant_id, row, spec), spec


async def _winners_token(tenant_id: str, connection_id: Any) -> str:
    """Another refresh (or a disconnect) won the conditional write; use the
    winner's token, or require a reconnect if it left the row not connected."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT access_token_ref FROM oauth_connections "
            "WHERE tenant_id = $1 AND id = $2 AND status = 'connected' AND deleted_at IS NULL",
            tenant_id, connection_id,
        )
    if row is None:
        raise ReconnectRequired
    return await auth_schemes.resolve_tenant_ref(tenant_id, row["access_token_ref"])


async def _refresh(tenant_id: str, row: Any, spec: OAuthProvider) -> str:
    refresh_token = await auth_schemes.resolve_tenant_ref(tenant_id, row["refresh_token_ref"])
    try:
        client_id, client_secret = await _client_credentials(spec)
        resp = await _provider_call("POST", spec.token_url.format(accounts_server=row["accounts_server"]), data={
            "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token,
        })
    except Exception:
        raise ValueError("credential_unavailable") from None

    pool = await db.get_pool()
    if resp.status_code == 400 and _error_code(resp) == "invalid_grant":
        async with tenant_conn(pool) as conn:
            flipped = await conn.execute(
                "UPDATE oauth_connections SET status = 'reconnect_needed', access_token_ref = NULL, "
                "       refresh_token_ref = NULL, access_expires_at = NULL, updated_at = now() "
                " WHERE tenant_id = $1 AND id = $2 AND refresh_token_ref = $3 AND status = 'connected'",
                tenant_id, row["id"], row["refresh_token_ref"],
            )
        if flipped == "UPDATE 0":
            return await _winners_token(tenant_id, row["id"])
        raise ReconnectRequired
    if resp.status_code != 200:
        raise ValueError("credential_unavailable")

    try:
        body = resp.json()
        access_token = body["access_token"]
        new_refresh = body.get("refresh_token")
        access_ref = encrypt_tenant_secret(tenant_id, access_token)
        new_refresh_ref = encrypt_tenant_secret(tenant_id, new_refresh) if new_refresh else None
        access_expires_at = _expires_at(body)
    except Exception:
        raise ValueError("credential_unavailable") from None

    async with tenant_conn(pool) as conn:
        updated = await conn.fetchrow(
            "UPDATE oauth_connections SET access_token_ref = $4, access_expires_at = $5, "
            "       refresh_token_ref = COALESCE($6, refresh_token_ref), updated_at = now() "
            " WHERE tenant_id = $1 AND id = $2 AND refresh_token_ref = $3 AND status = 'connected' "
            "RETURNING id",
            tenant_id, row["id"], row["refresh_token_ref"], access_ref, access_expires_at, new_refresh_ref,
        )
    if updated is None:
        return await _winners_token(tenant_id, row["id"])
    return access_token


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error")
    except ValueError:
        return None


async def disconnect(
    *, tenant_id: str, connection_id: str, user_id: str, user_email: str | None,
    background_tasks: BackgroundTasks,
) -> dict[str, bool]:
    """Foreground work is one conditional DB write plus the audit row, and the
    body is constant: whether the upstream revoke runs depends on whether
    another tenant holds the same grant, and neither the body nor the latency
    may reveal that (lesson 2). The shared-grant probe and the revoke run in
    `background_tasks`, after the response is sent."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "UPDATE oauth_connections c SET status = 'disconnected', access_token_ref = NULL, "
            "       refresh_token_ref = NULL, access_expires_at = NULL, updated_at = now() "
            "  FROM (SELECT id, provider, provider_sub, accounts_server, refresh_token_ref AS old_ref "
            "          FROM oauth_connections "
            "         WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL FOR UPDATE) o "
            " WHERE c.id = o.id AND c.tenant_id = $1 "
            "RETURNING o.provider, o.provider_sub, o.accounts_server, o.old_ref",
            tenant_id, connection_id,
        )
        if row is None:
            raise LookupError("oauth_connection not found")
        await audit.write_audit(
            conn, entity_type="oauth_connection", entity_id=connection_id, action="updated",
            user_id=user_id, user_email=user_email, new_value={"status": "disconnected"},
        )
    if row["old_ref"] is not None:
        background_tasks.add_task(
            _revoke_upstream, tenant_id=tenant_id, connection_id=str(connection_id), provider=row["provider"],
            provider_sub=row["provider_sub"], accounts_server=row["accounts_server"], old_ref=row["old_ref"],
        )
    return {"disconnected": True}


async def _revoke_upstream(
    *, tenant_id: str, connection_id: str, provider: str, provider_sub: str | None,
    accounts_server: str | None, old_ref: str,
) -> None:
    """Best effort, after the response. The DB was cleared first on purpose: a
    revoke failure must never leave a usable credential in storage."""
    spec = PROVIDERS[provider]
    if spec.revoke_url is None:
        return
    try:
        if provider_sub is not None:
            # The one deliberately cross-tenant read: $2 is the disconnecting
            # row's own subject, never a request field. A NULL subject means
            # revoke anyway.
            pool = await db.get_pool()
            async with platform_conn(pool, reason="oauth-shared-grant-check") as conn:
                shared = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM oauth_connections "
                    "WHERE provider = $1 AND provider_sub = $2 AND status = 'connected' "
                    "AND deleted_at IS NULL AND tenant_id <> $3)",
                    provider, provider_sub, tenant_id,
                )
            if shared:
                log.info("oauth_revoke_skipped_shared_grant", extra={"connection_id": connection_id})
                return
        refresh_token = await auth_schemes.resolve_tenant_ref(tenant_id, old_ref)
        await _provider_call(
            "POST", spec.revoke_url.format(accounts_server=accounts_server), data={"token": refresh_token},
        )
    except Exception:
        log.warning("oauth_revoke_failed", extra={"connection_id": connection_id})
