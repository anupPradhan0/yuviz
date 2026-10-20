"""
Knowledge Service — FastAPI app. Auth reuses services.config deps; JWT_SECRET must match Config Service.

Run: uvicorn services.knowledge.app:app --reload --port 8100
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import cache, db, retrieval_log
from .body_limit import BodyLimitMiddleware
from . import retrieval_plan as plans
from .routers import (
    agent_kb, documents, knowledge_bases, ops, prewarm, retrieval_policies, retrieve,
)
from .routers import health as tenant_health
from .runtime import get_embedding_manager, get_vector_repo, init_reranker

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail at startup on a broken POSTGRES_DSN, not on the first request.
    await db.get_pool()
    await get_vector_repo()
    get_embedding_manager()
    reranker = init_reranker()  # refuses to start on an unsafe KNOWLEDGE_RERANK_URL
    warm_task = asyncio.create_task(reranker.warm()) if hasattr(reranker, "warm") else None
    plans.startup()
    purge_task = asyncio.create_task(retrieval_log.purge_loop())
    legacy_task = asyncio.create_task(cache.purge_legacy_keys())
    yield
    if warm_task is not None:
        warm_task.cancel()
        await asyncio.gather(warm_task, return_exceptions=True)
    purge_task.cancel()
    try:
        await purge_task
    except asyncio.CancelledError:
        pass
    legacy_task.cancel()
    await asyncio.gather(legacy_task, return_exceptions=True)
    await plans.shutdown()  # background plan refreshes use the pool: stop them before it closes
    await retrieval_log.shutdown()  # before close_pool: the writer uses the pool
    await get_embedding_manager().aclose()  # keep-alive HTTP clients; also before the pool, in case a plan load is mid-flight
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Knowledge Service", lifespan=lifespan)

app.add_middleware(BodyLimitMiddleware)  # 413 for oversized /internal/retrieve and /internal/prewarm bodies

# Admin UI is the only browser client.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("ADMIN_UI_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(knowledge_bases.tenant_scoped_router)
app.include_router(knowledge_bases.router)
app.include_router(documents.router)
app.include_router(agent_kb.router)
app.include_router(retrieval_policies.router)
app.include_router(retrieve.router)
app.include_router(prewarm.router)
app.include_router(ops.router)
app.include_router(tenant_health.tenant_scoped_router)


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
