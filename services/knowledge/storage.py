"""
StorageProvider — where uploaded document bytes actually live. kb_documents.
source_ref is a StorageProvider-opaque pointer (a local path today, an S3/
GCS key once a cloud implementation exists); raw bytes never live in
Postgres. Swapping LocalStorageProvider for a cloud implementation later
touches one class, never documents.py/ingestion_worker.py, which only ever
call save()/read()/delete() against the StorageProvider protocol.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol


class StorageProvider(Protocol):
    async def save(self, tenant_id: str, kb_id: str, filename: str, content: bytes) -> str:
        """Returns a source_ref opaque to the caller — pass it back to
        read()/delete() unchanged; never parse or construct it elsewhere."""
        ...

    async def read(self, source_ref: str) -> bytes: ...

    async def delete(self, source_ref: str) -> None: ...


class LocalStorageProvider:
    """Stores documents on the local filesystem, one file per document under
    {root}/{tenant_id}/{kb_id}/{uuid}-{filename}. Fine for this single-node,
    single-MacBook deployment; the interface is what makes a future
    multi-node or cloud-storage move additive rather than a rewrite."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.environ.get("KNOWLEDGE_STORAGE_ROOT", "./data/knowledge_documents"))

    async def save(self, tenant_id: str, kb_id: str, filename: str, content: bytes) -> str:
        directory = self._root / tenant_id / kb_id
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4()}-{Path(filename).name}"
        path = directory / safe_name
        path.write_bytes(content)
        return str(path)

    async def read(self, source_ref: str) -> bytes:
        return Path(source_ref).read_bytes()

    async def delete(self, source_ref: str) -> None:
        Path(source_ref).unlink(missing_ok=True)
