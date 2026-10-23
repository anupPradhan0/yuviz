"""Clinic Service — doctors, OPD schedules and appointments behind the clinic agent's tools.

Run: uvicorn services.clinic.app:app --port 8700
Workers (outbox delivery, reminder calls) start with the app unless CLINIC_WORKERS=0.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import booking, db
from .outbox import OutboxWorker
from .reminders import ReminderWorker
from .routers import admin, tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

_workers_enabled = os.environ.get("CLINIC_WORKERS", "1") != "0"
_outbox = OutboxWorker()
_reminders = ReminderWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_pool()
    if _workers_enabled:
        _outbox.start()
        _reminders.start()
    yield
    if _workers_enabled:
        await _reminders.stop()
        await _outbox.stop()
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Clinic Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("ADMIN_UI_ORIGINS", "http://localhost:3000").split(",")
                   if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tools.router)
app.include_router(admin.tenant_scoped_router)


@app.exception_handler(booking.ClinicNotConfigured)
async def not_configured_handler(request: Request, exc: booking.ClinicNotConfigured) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "clinic is not set up for this tenant"})


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "not found"})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
