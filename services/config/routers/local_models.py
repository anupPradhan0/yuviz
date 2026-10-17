"""Free local models: what this host has and downloading more. Host-wide, so no tenant scope."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..auth import CurrentUser
from ..deps import get_current_user, require_role
from ..model_downloads import DownloadCapReached
from ..schemas import LocalModelDownload

router = APIRouter(prefix="/local-models", tags=["local_models"])


@router.get("")
async def list_local_models(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    return await request.app.state.model_downloads.status()


@router.post("/downloads", status_code=202)
async def download_local_model(
    body: LocalModelDownload,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    try:
        status = await request.app.state.model_downloads.trigger(body.engine, body.name)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except DownloadCapReached as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    if status == "ready":
        response.status_code = 200
    return {"status": status}
