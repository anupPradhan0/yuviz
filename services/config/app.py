"""
Config Service — FastAPI app. A thin HTTP wrapper around tenants.py/agents.py/
provider_configs.py: routers translate HTTP <-> those functions and nothing
else. All business logic (caching, audit, config versioning) already lives
in those modules and in the database triggers — this file has none of its
own.

Run: uvicorn services.config.app:app --reload
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from libs.config_sdk.secrets import SecretEncryptionUnavailable

from . import cache, db, invites
from . import phone_numbers as phone_numbers_service
from .routers import (
    agent_tool_policies, agents, audit_log, auth, calls, carriers, invites as invites_router,
    phone_numbers, provider_configs, telephony_configs, tenants, tool_catalog,
    tool_provider_configs, users,
)

log = logging.getLogger(__name__)


class FixedWindowCounter:
    """Per-process, per-worker fixed-window rate counter — no Redis, resets
    on restart, and a multi-replica deployment multiplies every limit by
    the replica count (accepted: Config Service runs single-replica today,
    see design doc's throttle risk). `over_limit` only peeks; callers that
    need outcome-blind counting (the invite probe cap) call `increment`
    unconditionally themselves rather than relying on this class to do it
    for them on every check.

    `_buckets` entries are swept on every access, not just reset in place —
    without that, a key that's been reset in place but never deleted is a
    permanent dict entry, and AcceptThrottle keys on the client IP of two
    *public, unauthenticated* routes, so every distinct source address that
    ever hits them would otherwise leak one entry forever. The sweep bounds
    the map to keys seen within the current window, not the process
    lifetime."""

    def __init__(self, *, limit: int, window_seconds: float):
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}

    def _evict_stale(self, now: float) -> None:
        stale = [k for k, (window_start, _) in self._buckets.items() if now - window_start >= self._window]
        for k in stale:
            del self._buckets[k]

    def _current(self, key: str, now: float) -> tuple[float, int]:
        self._evict_stale(now)
        window_start, count = self._buckets.get(key, (now, 0))
        if now - window_start >= self._window:
            window_start, count = now, 0
        return window_start, count

    def over_limit(self, key: str) -> tuple[bool, int]:
        """Returns (over, retry_after_seconds). Does not increment."""
        now = time.monotonic()
        window_start, count = self._current(key, now)
        if count >= self._limit:
            return True, int(self._window - (now - window_start)) + 1
        return False, 0

    def increment(self, key: str) -> None:
        now = time.monotonic()
        window_start, count = self._current(key, now)
        self._buckets[key] = (window_start, count + 1)


class InviteThrottle:
    """The two counters from the design's "Probe rate limit" section, both
    keyed on the acting admin's JWT subject (`invited_by`) and both checked
    before invites.create_invite's users lookup runs.

    - probe: 30 create attempts/hour, outcome-blind — incremented
      unconditionally by the caller regardless of what create_invite does
      with the request, so a 409 costs exactly what a 201 costs.
    - send: 20 successful sends/hour across create+resend (the mail-bomb
      cap) — only incremented after a create/resend actually succeeds.
    """

    def __init__(self) -> None:
        self.probe = FixedWindowCounter(limit=30, window_seconds=3600)
        self.send = FixedWindowCounter(limit=20, window_seconds=3600)

    def check_probe(self, actor_id: str) -> None:
        over, retry_after = self.probe.over_limit(actor_id)
        if over:
            raise _too_many_requests("too many invite attempts; try again later", retry_after)
        self.probe.increment(actor_id)

    def check_send_cap(self, actor_id: str) -> None:
        over, retry_after = self.send.over_limit(actor_id)
        if over:
            raise _too_many_requests("too many invites sent this hour; try again later", retry_after)

    def record_send(self, actor_id: str) -> None:
        self.send.increment(actor_id)


class AcceptThrottle:
    """The accept-route IP throttle (design's "Throttle key" section): 10/
    minute and 50/hour, covering GET+POST together, keyed on
    `request.client.host` — **never** `X-Forwarded-For`, which is entirely
    attacker-controlled here (no reverse proxy sits in front of this
    service in deployment/docker/docker-compose.yml; the Admin UI calls
    http://localhost:8000 directly). If a proxy is introduced later, key on
    the right-most untrusted hop of X-Forwarded-For behind an explicit
    trusted_hosts list — never the left-most, and never the header
    unvalidated."""

    def __init__(self) -> None:
        self.minute = FixedWindowCounter(limit=10, window_seconds=60)
        self.hour = FixedWindowCounter(limit=50, window_seconds=3600)

    def check(self, client_host: str) -> None:
        over, retry_after = self.minute.over_limit(client_host)
        if not over:
            over, retry_after = self.hour.over_limit(client_host)
        if over:
            raise _too_many_requests("too many attempts; try again later", retry_after)
        self.minute.increment(client_host)
        self.hour.increment(client_host)


def _too_many_requests(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect eagerly, not lazily, so a broken POSTGRES_DSN/REDIS_URL fails at
    # startup — the same "fail fast, not on the first request" reasoning as
    # AIProviderManager.prewarm().
    await db.get_pool()
    cache.get_client()
    # Populate did:{did} for every active DID now — see
    # phone_numbers.py's top-of-file comment for the full design.
    warmed = await phone_numbers_service.prewarm()
    log.info("Prewarmed %d active phone number(s) into Redis", warmed)
    yield
    await db.close_pool()
    await cache.close()


app = FastAPI(title="Voice AI Platform — Config Service", lifespan=lifespan)

# Admin UI (admin-ui/, Next.js dev server) is the only browser client — this
# is a local-only dev tool, so the origin list stays narrow rather than a
# wildcard. Real request-scoped auth (JWT, see auth.py/deps.py) is enforced
# per-route now, not by CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-process throttle state for the invite routers — see InviteThrottle/
# AcceptThrottle above. Attached to app.state (not module-level singletons
# imported by routers/invites.py) so this file stays the one place that
# constructs them, with no import cycle back from the router it mounts.
app.state.invite_throttle = InviteThrottle()
app.state.accept_throttle = AcceptThrottle()

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(invites_router.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(provider_configs.tenant_scoped_router)
app.include_router(provider_configs.router)
app.include_router(phone_numbers.tenant_scoped_router)
app.include_router(phone_numbers.router)
app.include_router(carriers.tenant_scoped_router)
app.include_router(carriers.router)
app.include_router(calls.tenant_scoped_router)
app.include_router(calls.router)
app.include_router(tool_provider_configs.tenant_scoped_router)
app.include_router(tool_provider_configs.router)
app.include_router(agent_tool_policies.router)
app.include_router(tool_catalog.router)
app.include_router(telephony_configs.tenant_scoped_router)
app.include_router(telephony_configs.router)
app.include_router(telephony_configs.providers_router)
app.include_router(audit_log.router)


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(SecretEncryptionUnavailable)
async def _secret_encryption_unavailable(_request, exc: SecretEncryptionUnavailable):
    # A server misconfiguration, not a bad request — but the message says
    # exactly what to set, so it has to reach the caller rather than 500.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(
    request: Request, exc: asyncpg.ForeignKeyViolationError,
) -> JSONResponse:
    # Defense in depth: routers should validate referenced ids exist before
    # inserting (see provider_configs router's _resolve_tenant_id) — this
    # catches whatever a future route forgets to, so a bad foreign key is
    # always a clean 400, never a raw Postgres constraint name reaching the
    # client.
    return JSONResponse(
        status_code=400,
        content={"detail": "request references an id that does not exist"},
    )


@app.exception_handler(invites.PermissionDenied)
async def _invite_permission_denied(request: Request, exc: invites.PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "not permitted to invite this role/tenant"})


@app.exception_handler(invites.EmailConflict)
async def _invite_email_conflict(request: Request, exc: invites.EmailConflict) -> JSONResponse:
    # Byte-identical for every tenant_admin case (round-1 CRITICAL, see
    # design doc's "Conflict responses") — tenant_name is None unless the
    # actor was a super_admin, checked by invites._conflict_tenant_name.
    if exc.tenant_name is not None:
        detail = f"email already belongs to tenant '{exc.tenant_name}'"
    else:
        detail = "this email cannot be invited"
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(invites.InviteNotPending)
async def _invite_not_pending(request: Request, exc: invites.InviteNotPending) -> JSONResponse:
    # Covers resend/revoke of an already-accepted or already-revoked invite.
    # Known gap (see design doc + PR notes): an already-*accepted* invite
    # hits this same branch — revoking it is refused rather than having any
    # effect on the user it already created. Reported, not fixed, here.
    return JSONResponse(status_code=409, content={"detail": "invite is not pending"})


@app.exception_handler(invites.ResendCooldown)
async def _invite_resend_cooldown(request: Request, exc: invites.ResendCooldown) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"invite was just sent; try again in {exc.remaining_seconds}s"},
        headers={"Retry-After": str(exc.remaining_seconds)},
    )


@app.exception_handler(invites.InviteExpired)
async def _invite_expired(request: Request, exc: invites.InviteExpired) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has expired"})


@app.exception_handler(invites.InviteRevoked)
async def _invite_revoked(request: Request, exc: invites.InviteRevoked) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has been revoked"})


@app.exception_handler(invites.InviteUsed)
async def _invite_used(request: Request, exc: invites.InviteUsed) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has already been used"})


@app.exception_handler(invites.InviteContextGone)
async def _invite_context_gone(request: Request, exc: invites.InviteContextGone) -> JSONResponse:
    # Tenant-blind by construction (see invites.InviteContextGone docstring)
    # — the accepter learns only that the invite is no longer valid.
    return JSONResponse(status_code=410, content={"detail": "invite is no longer valid"})


@app.exception_handler(invites.EmailTaken)
async def _invite_email_taken(request: Request, exc: invites.EmailTaken) -> JSONResponse:
    # Same tenant-blind wording as the create-path conflict — the accepting
    # party is unauthenticated and must learn nothing about the other
    # tenant (design doc's "Step 3's own conflict").
    return JSONResponse(status_code=409, content={"detail": "an account already exists for this email"})


@app.get("/health")
async def health():
    return {"status": "ok"}
