"""
Knowledge Service — FastAPI app. Auth reuses services.config deps; JWT_SECRET must match Config Service.

Run: uvicorn services.knowledge.app:app --reload --port 8100
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import cache, db, retrieval_log
from .body_limit import BodyLimitMiddleware
from . import retrieval_plan as plans
from .routers import agent_kb, documents, knowledge_bases, prewarm, retrieval_policies, retrieve
from .runtime import get_embedding_manager, get_vector_repo

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail at startup on a broken POSTGRES_DSN, not on the first request.
    await db.get_pool()
    await get_vector_repo()
    get_embedding_manager()
    plans.startup()
    purge_task = asyncio.create_task(retrieval_log.purge_loop())
    legacy_task = asyncio.create_task(cache.purge_legacy_keys())
    yield
    purge_task.cancel()
    try:
        await purge_task
    except asyncio.CancelledError:
        pass
    legacy_task.cancel()
    await asyncio.gather(legacy_task, return_exceptions=True)
    await plans.shutdown()  # background plan refreshes use the pool: stop them before it closes
    await retrieval_log.shutdown()  # before close_pool: the writer uses the pool
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Knowledge Service", lifespan=lifespan)

app.add_middleware(BodyLimitMiddleware)  # 413 for oversized /internal/retrieve and /internal/prewarm bodies

# Admin UI is the only browser client.
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
app.include_router(prewarm.router)


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
