"""Internal calendar-event endpoint for backend services that mirror their records to Google.

Gated like /internal/chains/execute: a named service identity, not just any service account.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import get_current_user

from .. import calendar_sync

router = APIRouter(prefix="/internal/calendar", tags=["calendar"])

_CALENDAR_SUBJECTS = frozenset(
    e.strip().lower() for e in os.environ.get(
        "TOOLEXEC_CALENDAR_SUBJECTS", "clinic-service@internal.yuviz.ai",
    ).split(",") if e.strip()
)


async def require_calendar_subject(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not (user.is_service_account and user.email.lower() in _CALENDAR_SUBJECTS):
        raise HTTPException(status_code=403, detail="identity may not write calendar events")
    return user


class CalendarEventSync(BaseModel):
    tenant_id: str
    connection_id: str
    calendar_id: str = Field(min_length=3, max_length=255)
    # Google's own rule for client-chosen ids: base32hex characters, 5 to 1024 of them.
    event_id: str = Field(pattern=r"^[a-v0-9]{5,1024}$")
    event: dict[str, Any] | None = None   # None deletes the event


@router.post("/events/sync")
async def sync_event(
    body: CalendarEventSync, current_user: CurrentUser = Depends(require_calendar_subject),
) -> dict[str, Any]:
    if current_user.tenant_id is not None and current_user.tenant_id != body.tenant_id:
        raise HTTPException(status_code=403, detail="identity may not write calendar events")
    set_target_tenant(body.tenant_id)
    try:
        return await calendar_sync.sync_event(
            tenant_id=body.tenant_id, connection_id=body.connection_id, calendar_id=body.calendar_id,
            event_id=body.event_id, event=body.event,
        )
    except calendar_sync.CalendarSyncFailed as exc:
        raise HTTPException(status_code=503 if exc.retryable else 422, detail=exc.code) from None
