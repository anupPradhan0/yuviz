"""
FastAPI app tying the Vobiz webhook + WebSocket protocol together — the
one process-shape difference from services/webcall/ (a plain `websockets`
server): Vobiz needs both HTTP POST endpoints (answer/hangup/ring
webhooks) and a WebSocket endpoint in the same service, which FastAPI
gives us in one place instead of two separate servers.

Telephony provider credentials are resolved per-tenant from Config
Service's telephony_configs (see libs/telephony_sdk/ and
services/config/telephony_configs.py) instead of the single hardcoded
VOBIZ_AUTH_ID/VOBIZ_AUTH_TOKEN env vars this file used before — this is
what makes the bridge usable by more than one tenant's Vobiz account, and
by a future non-Vobiz provider without touching this file again (the
provider instance is looked up via TelephonyProviderRegistry by whatever
`provider` string that tenant's default telephony_configs row has).

Call metadata (tenant_slug, agent_slug, direction, DIDs) is tracked in an
in-memory dict keyed by call_uuid — populated synchronously from
place_call()'s return value for outbound calls, or from the inbound
answer webhook's own CallUUID/To fields (via Redis DID resolution) for
inbound calls — before the WebSocket ever connects and looks it up. This
process is not meant to be horizontally scaled; if it ever needs to be,
this dict becomes a Redis hash the same way the Gateway already treats
routing data, not sooner. Resolved provider instances are cached the same
way, keyed by tenant_slug — re-fetching telephony_configs on every single
webhook would be needless per-request Config Service load for credentials
that essentially never change mid-call.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from libs.telephony_sdk.interface import ITelephonyProvider
from libs.telephony_sdk.providers import vobiz as _vobiz_provider  # noqa: F401 — registers providers
from libs.telephony_sdk.registry import TelephonyProviderRegistry

from . import redis_route
from .bridge import VobizCallBridge

log = logging.getLogger("vobiz.app")

app = FastAPI(title="Vobiz Bridge")

PUBLIC_BASE_URL = os.environ["VOBIZ_PUBLIC_BASE_URL"].rstrip("/")  # e.g. https://xxxx.ngrok-free.app
CONFIG_SERVICE_URL = os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000").rstrip("/")
_SERVICE_EMAIL = os.environ.get("CONFIG_SERVICE_EMAIL", "conversation-service@internal.yuviz.ai")
_SERVICE_PASSWORD = os.environ.get("CONFIG_SERVICE_PASSWORD", "")

_jwt_token: str | None = None
_provider_cache: dict[str, ITelephonyProvider] = {}  # tenant_slug -> provider instance


async def _login() -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{CONFIG_SERVICE_URL}/auth/login",
            json={"email": _SERVICE_EMAIL, "password": _SERVICE_PASSWORD},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _config_get(path: str) -> dict | list | None:
    """GET against Config Service with the service-account JWT, re-logging
    in once on a 401 (expired/invalid token) before giving up."""
    global _jwt_token
    if _jwt_token is None:
        _jwt_token = await _login()

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{CONFIG_SERVICE_URL}{path}", headers={"Authorization": f"Bearer {_jwt_token}"})
        if resp.status_code == 401:
            _jwt_token = await _login()
            resp = await client.get(f"{CONFIG_SERVICE_URL}{path}", headers={"Authorization": f"Bearer {_jwt_token}"})

    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def resolve_provider(tenant_slug: str) -> ITelephonyProvider | None:
    """Tenant's default-outbound telephony_configs row -> a live provider
    instance, cached per tenant_slug for the process lifetime."""
    cached = _provider_cache.get(tenant_slug)
    if cached is not None:
        return cached

    tenant = await _config_get(f"/tenants/{tenant_slug}")
    if tenant is None:
        log.warning("vobiz: resolve_provider: no such tenant=%s", tenant_slug)
        return None

    configs = await _config_get(f"/tenants/{tenant['id']}/telephony-configs")
    if not configs:
        log.warning("vobiz: resolve_provider: tenant=%s has no telephony_configs", tenant_slug)
        return None
    default_config = next((c for c in configs if c["is_default_outbound"]), configs[0])

    provider_cls = TelephonyProviderRegistry.get(default_config["provider"])
    credentials = default_config["credentials"]
    if isinstance(credentials, str):  # JSONB round-trips as a raw string without a codec (see db.py)
        credentials = json.loads(credentials)
    provider = provider_cls(credentials)
    _provider_cache[tenant_slug] = provider
    return provider


@dataclass
class CallMeta:
    tenant_slug: str
    agent_slug: str
    direction: str
    caller_did: str = ""
    called_did: str = ""


_calls: dict[str, CallMeta] = {}


@app.post("/vobiz/answer")
async def answer(request: Request) -> Response:
    """The answer_url webhook — fires for both outbound calls we placed
    (metadata already populated by /vobiz/call below) and genuine inbound
    calls (metadata resolved here, from-scratch, via the DID). Tenant is
    resolved BEFORE signature verification (a read-only Redis lookup, safe
    to do first) since verification itself needs to know which tenant's
    credentials to check against — a real requirement now that credentials
    are per-tenant instead of one single global auth_token."""
    form = await request.form()
    call_uuid = form.get("CallUUID") or form.get("call_uuid")
    to_number = form.get("To") or form.get("to")
    from_number = form.get("From") or form.get("from")

    if not call_uuid:
        return PlainTextResponse("missing CallUUID", status_code=400)

    meta = _calls.get(call_uuid)
    if meta is None:
        # Genuine inbound call — resolve the dialed number the same way
        # the Gateway resolves a DID: Redis only, never Config Service
        # synchronously on this call path.
        tenant_slug, agent_slug = await redis_route.resolve_did(str(to_number))
        meta = CallMeta(
            tenant_slug=tenant_slug,
            agent_slug=agent_slug,
            direction="inbound",
            caller_did=str(from_number or ""),
            called_did=str(to_number or ""),
        )
        _calls[call_uuid] = meta
        log.info("vobiz: inbound call=%s to=%s -> tenant=%s agent=%s", call_uuid, to_number, tenant_slug, agent_slug)

    provider = await resolve_provider(meta.tenant_slug)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if provider is None or not provider.verify_webhook_signature(str(request.url), headers):
        log.warning("vobiz: answer webhook failed signature verification call=%s tenant=%s", call_uuid, meta.tenant_slug)
        return PlainTextResponse("invalid signature", status_code=403)

    ws_url = f"{PUBLIC_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/vobiz/ws/{call_uuid}"
    xml = provider.build_answer_response(ws_url)
    return Response(content=xml, media_type="application/xml")


@app.post("/vobiz/hangup-callback")
async def hangup_callback(request: Request) -> Response:
    form = await request.form()
    call_uuid = form.get("CallUUID") or form.get("call_uuid")
    meta = _calls.get(call_uuid) if call_uuid else None
    if meta is None:
        # No tracked metadata means no tenant to verify against — fail
        # closed rather than guess whose credentials to check.
        return PlainTextResponse("unknown call", status_code=403)

    provider = await resolve_provider(meta.tenant_slug)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if provider is None or not provider.verify_webhook_signature(str(request.url), headers):
        return PlainTextResponse("invalid signature", status_code=403)

    _calls.pop(call_uuid, None)
    log.info("vobiz: call=%s hung up, metadata dropped", call_uuid)
    return PlainTextResponse("ok")


@app.post("/vobiz/ring-callback")
async def ring_callback(request: Request) -> Response:
    form = await request.form()
    call_uuid = form.get("CallUUID") or form.get("call_uuid")
    meta = _calls.get(call_uuid) if call_uuid else None
    if meta is None:
        return PlainTextResponse("unknown call", status_code=403)

    provider = await resolve_provider(meta.tenant_slug)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if provider is None or not provider.verify_webhook_signature(str(request.url), headers):
        return PlainTextResponse("invalid signature", status_code=403)
    return PlainTextResponse("ok")


@app.post("/vobiz/call")
async def place_call(request: Request) -> dict:
    """Internal trigger for outbound test calls (and, later, Campaign
    Service) — not a Vobiz webhook, so no signature check applies here."""
    body = await request.json()
    to_number = body["to"]
    tenant_slug = body["tenant_slug"]
    agent_slug = body["agent_slug"]

    provider = await resolve_provider(tenant_slug)
    if provider is None:
        return {"ok": False, "error": f"no telephony_config for tenant {tenant_slug!r}"}

    from_number = body.get("from", "")
    try:
        call_uuid = await provider.initiate_call(
            from_number=from_number,
            to_number=to_number,
            answer_url=f"{PUBLIC_BASE_URL}/vobiz/answer",
            hangup_url=f"{PUBLIC_BASE_URL}/vobiz/hangup-callback",
            ring_url=f"{PUBLIC_BASE_URL}/vobiz/ring-callback",
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    _calls[call_uuid] = CallMeta(
        tenant_slug=tenant_slug, agent_slug=agent_slug, direction="outbound",
        caller_did=from_number, called_did=to_number,
    )
    return {"ok": True, "call_uuid": call_uuid}


@app.websocket("/vobiz/ws/{call_uuid}")
async def vobiz_ws(websocket: WebSocket, call_uuid: str) -> None:
    await websocket.accept()
    meta = _calls.get(call_uuid)
    if meta is None:
        log.warning("vobiz: WS connected for unknown call=%s, using default route", call_uuid)
        meta = CallMeta(tenant_slug="default", agent_slug="default", direction="inbound")

    bridge = VobizCallBridge(
        call_uuid=call_uuid,
        tenant_slug=meta.tenant_slug,
        agent_slug=meta.agent_slug,
        direction=meta.direction,
        caller_did=meta.caller_did,
        called_did=meta.called_did,
    )
    try:
        await bridge.run(websocket)
    except WebSocketDisconnect:
        log.info("vobiz: WS disconnected call=%s", call_uuid)
    # _calls is NOT popped here: hangup-callback (which arrives as a
    # separate HTTP webhook, often just after WS teardown) needs this
    # entry to know which tenant's credentials to verify its own signature
    # against. hangup_callback() is the sole owner of removing an entry —
    # popping it here too raced it and caused real, confirmed-live 403s on
    # an otherwise-legitimate hangup-callback.
