"""
DID -> tenant/agent resolution, read directly from Redis — mirrors the
Gateway's own PhoneRoute::from_redis() exactly (gateway/include/telephony),
same "did:{did}" key shape, same fallback to {"default","default"} on a
miss. This bridge sits on the real-time call path (a real inbound Vobiz
call is waiting on this lookup before audio can start), so it follows the
platform's hot-path rule: Redis only, never Config Service/Postgres here
— see project memory "architecture_decisions_voiceai" and
"phase5_coding_rules".
"""

from __future__ import annotations

import json
import logging
import os

import redis.asyncio as redis

log = logging.getLogger(__name__)

_DEFAULT_TENANT = "default"
_DEFAULT_AGENT = "default"

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _client = redis.from_url(url, decode_responses=True)
    return _client


async def resolve_did(did: str) -> tuple[str, str]:
    """Returns (tenant_slug, agent_slug). An unrecognized DID (never
    provisioned, or Redis unreachable) resolves to the default tenant/agent
    — same "never a rejected call" posture as the Gateway, never an
    exception on the call path."""
    try:
        raw = await _get_client().get(f"did:{did}")
    except redis.RedisError:
        log.warning("resolve_did: Redis unreachable, falling back to default did=%s", did)
        return _DEFAULT_TENANT, _DEFAULT_AGENT

    if raw is None:
        log.info("resolve_did: no route for did=%s, falling back to default", did)
        return _DEFAULT_TENANT, _DEFAULT_AGENT

    try:
        route = json.loads(raw)
        return route["tenant_slug"], route["agent_slug"]
    except (json.JSONDecodeError, KeyError):
        log.warning("resolve_did: malformed route did=%s raw=%r", did, raw)
        return _DEFAULT_TENANT, _DEFAULT_AGENT
