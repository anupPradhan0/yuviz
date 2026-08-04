from __future__ import annotations

import os

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("KNOWLEDGE_STORAGE_ROOT", "/tmp/voiceai-knowledge-test-storage")

import pytest_asyncio

from services.config import db as config_db  # noqa: E402
from services.knowledge import db as knowledge_db  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await config_db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def knowledge_pool():
    p = await knowledge_db.get_pool()
    yield p
