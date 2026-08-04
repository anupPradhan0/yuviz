"""
provider_config_subscriber.py — subscribes to Config Service's Redis
Pub/Sub channel (services/config/provider_configs.py's
PROVIDER_CONFIG_CHANGED_CHANNEL) so editing a provider_config evicts that
one cached instance in THIS process's AIProviderManager instantly, instead
of needing a full process restart (which drops every live call on that
instance — see project history, 2026-07-29).

Runs as its own background asyncio task (see __main__.py) — shares no
lock/state with the per-call pipeline, so a slow or unreachable Redis can
never add latency to a live call. Same reconnect-with-backoff discipline
as services/campaigns/originate.py's EslJobEventListener.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis

from .ai_provider_manager import AIProviderManager

log = logging.getLogger(__name__)

CHANNEL = "provider_config_changed"


class ProviderConfigSubscriber:
    def __init__(self, redis_url: str, provider_manager: AIProviderManager) -> None:
        self._redis_url = redis_url
        self._provider_manager = provider_manager
        self._stopped = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ProviderConfigSubscriber: connection lost, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _listen_once(self) -> None:
        client = redis.from_url(self._redis_url, decode_responses=True)
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            log.info("ProviderConfigSubscriber: subscribed to %s", CHANNEL)
            async for message in pubsub.listen():
                if self._stopped:
                    break
                if message.get("type") != "message":
                    continue  # the subscribe confirmation itself arrives as a "subscribe" message
                config_id = message["data"]
                evicted = self._provider_manager.invalidate(config_id)
                if evicted:
                    log.info(
                        "ProviderConfigSubscriber: invalidated cached provider config_id=%s — "
                        "next call using it will reconstruct from the new config",
                        config_id,
                    )
        finally:
            await client.aclose()
