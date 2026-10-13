"""GET /tenants/{tenant_id}/knowledge/health: ingestion queue and retrieval numbers for the admin UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from services.config.auth import CurrentUser
from services.config.deps import bind_path_tenant, get_current_user, require_path_tenant_access

from .. import health as health_service

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/knowledge",
    tags=["knowledge_health"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)


@tenant_scoped_router.get("/health")
async def knowledge_health(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await health_service.tenant_health()
