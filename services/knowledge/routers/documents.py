from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, require_role

from .. import documents as documents_service
from .. import knowledge_bases as kb_service
from ..schemas import DocumentUpdate
from ..storage import LocalStorageProvider

router = APIRouter(tags=["kb_documents"])
_storage = LocalStorageProvider()


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await documents_service.list_documents(kb_id)


@router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    language: str | None = Form(default=None),
    tags: str = Form(default="{}"),
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    kb = await kb_service.get_knowledge_base(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"knowledge_base {kb_id!r} not found")

    content = await file.read()
    return await documents_service.upload_document(
        tenant_id=kb["tenant_id"],
        kb_id=kb_id,
        title=title,
        filename=file.filename or "document",
        content_type=file.content_type or "application/octet-stream",
        content=content,
        storage=_storage,
        language=language,
        tags=json.loads(tags),
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/documents/{document_id}")
async def get_document(document_id: str, current_user: CurrentUser = Depends(get_current_user)):
    document = await documents_service.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"kb_document {document_id!r} not found")
    return document


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: str,
    body: DocumentUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await documents_service.update_document(
        document_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await documents_service.soft_delete_document(
        document_id, user_id=current_user.id, user_email=current_user.email,
    )
