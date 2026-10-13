"""Read-only kbflag2:{len(tenant)}:{tenant}:{agent} -> "1"|"0" flag, written by Knowledge Service.

Lets non-RAG agents skip the HTTP round trip entirely. The length prefix keeps the key injective (slugs have no
charset constraint); services/knowledge/cache.py writes the same key and a test pins the two together.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis

log = logging.getLogger(__name__)


def agent_kb_flag_key(tenant_slug: str, agent_slug: str) -> str:
    return f"kbflag2:{len(tenant_slug)}:{tenant_slug}:{agent_slug}"


class RedisKnowledgeRepository:
    def __init__(self, redis_url: str) -> None:
        self._client = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool | None:
        try:
            raw = await self._client.get(agent_kb_flag_key(tenant_slug, agent_slug))
        except redis.RedisError:
            log.warning(
                "RedisKnowledgeRepository: Redis unreachable, treating as miss "
                "tenant=%s agent=%s", tenant_slug, agent_slug,
            )
            return None
        return None if raw is None else raw == "1"
