"""
A Redis outage must never propagate out of get_json/set_json/invalidate —
callers (tenants.py, agents.py, provider_configs.py) rely on this to degrade
to "every read hits Postgres" rather than fail the request outright. See
cache.py's module docstring.

cache._client is a lazily-created module-level singleton shared across the
whole test session (asyncio_default_fixture_loop_scope=session) — these
tests swap it out for a client pointing at an unreachable port, then restore
whatever was there before, rather than touching the real shared client other
tests depend on.
"""

from __future__ import annotations

import redis.asyncio as redis

from services.config import cache


async def _with_unreachable_client(coro_factory):
    original = cache._client
    cache._client = redis.from_url("redis://localhost:9999/0", decode_responses=True)
    try:
        return await coro_factory()
    finally:
        await cache._client.aclose()
        cache._client = original


async def test_get_json_returns_none_on_redis_outage_instead_of_raising():
    result = await _with_unreachable_client(lambda: cache.get_json("some:key"))
    assert result is None


async def test_set_json_does_not_raise_on_redis_outage():
    # No exception is the assertion — a raised RedisError would fail the test.
    await _with_unreachable_client(lambda: cache.set_json("some:key", {"a": 1}))


async def test_invalidate_does_not_raise_on_redis_outage():
    await _with_unreachable_client(lambda: cache.invalidate("some:key"))


async def test_publish_does_not_raise_on_redis_outage():
    await _with_unreachable_client(lambda: cache.publish("some_channel", "some message"))
