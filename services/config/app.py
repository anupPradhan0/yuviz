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
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from libs.config_sdk.secrets import SecretEncryptionUnavailable

from . import cache, db
from . import phone_numbers as phone_numbers_service
from .routers import (
    agent_tool_policies, agents, audit_log, auth, calls, carriers, phone_numbers, provider_configs,
    telephony_configs, tenants, tool_catalog, tool_provider_configs, users,
)

log = logging.getLogger(__name__)


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

app.include_router(auth.router)
app.include_router(users.router)
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


@app.get("/health")
async def health():
    return {"status": "ok"}
