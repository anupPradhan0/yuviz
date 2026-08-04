"""
Knowledge Service — FastAPI app. Thin HTTP wrapper around knowledge_bases.py/
documents.py/agent_kb.py/retrieval.py, same "routers translate, business
logic lives in the modules" convention as services/config/app.py.

Auth: imports services.config.auth/deps directly (JWT decode/CurrentUser/
require_role) rather than a shared libs/auth_sdk — an explicit, temporary
choice for this phase (see project memory: Phase 6A scope). Both services
must share the same JWT_SECRET env var for a token minted by Config
Service's /auth/login to validate here.

Run: uvicorn services.knowledge.app:app --reload --port 8100
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .routers import agent_kb, documents, knowledge_bases, retrieval_policies, retrieve
from .runtime import get_embedding_manager, get_vector_repo

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect eagerly so a broken POSTGRES_DSN fails at startup, not on the
    # first request — same reasoning as services/config/app.py's lifespan.
    await db.get_pool()
    await get_vector_repo()
    get_embedding_manager()
    yield
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Knowledge Service", lifespan=lifespan)

# Admin UI is the only browser client — same narrow local-dev origin list as
# Config Service's app.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(knowledge_bases.tenant_scoped_router)
app.include_router(knowledge_bases.router)
app.include_router(documents.router)
app.include_router(agent_kb.router)
app.include_router(retrieval_policies.router)
app.include_router(retrieve.router)


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
