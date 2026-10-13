"""GET /internal/ops: process-level counters (limiter overload, breaker states, plan-cache hits, log drops).
The repo has no /metrics convention (conversation and gateway use an injected IMetrics sink), so this is a JSON
endpoint, with ?format=prometheus for a scraper. Platform-scoped callers only: the numbers are about the process,
not a tenant, and a tenant admin has no business reading another tenant's load."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, is_platform_scoped

from .. import ops_metrics

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/ops")
async def ops(
    format: Literal["json", "prometheus"] = Query(default="json"),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not is_platform_scoped(current_user):
        raise HTTPException(status_code=403, detail="process metrics are platform-scoped")
    if format == "prometheus":
        return PlainTextResponse(ops_metrics.prometheus_text(), media_type="text/plain; version=0.0.4")
    return ops_metrics.snapshot()
