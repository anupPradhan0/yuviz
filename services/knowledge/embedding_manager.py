"""
EmbeddingProviderManager — creates, caches, and resolves secrets for
embedding provider instances, one per distinct provider_configs row with
role='embedding'. Deliberately a new, small, parallel manager rather than a
reuse of services.conversation.ai_provider_manager.AIProviderManager: that
manager lives in Conversation Service's package, and Knowledge Service must
not import it (same microservice-boundary reasoning as db.py/cache.py/
secret_resolver.py — the "don't extract libs/auth_sdk yet" instruction's
spirit applies equally here: a little duplication now, no premature shared
library). The pattern (registry dict keyed by engine, per-id caching,
secrets resolved once at instantiation) is intentionally copied, though —
it is already proven, non-negotiable-latency-rule-compliant design.

Two engines registered today:
  - "ollama"  — local, no API key, calls http://localhost:11434/api/embeddings
               with model "nomic-embed-text" (768-dim — matches kb_chunks.
               embedding's column width; see database/knowledge_schema.sql).
  - "openai"  — cloud, requires api_key_ref. Not usable end-to-end on this
               machine today (768 vs 1536 dims — see schema comment) but
               registered so the registry-extension pattern is real, not
               hypothetical.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .secret_resolver import SecretResolver


class IEmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingProviderConfig:
    id: str
    engine: str
    model: str | None = None
    api_key_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class OllamaEmbeddingProvider:
    def __init__(self, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434") -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Ollama's /api/embeddings takes one prompt per call — no batch
        # endpoint exists for this API today, so N texts means N requests.
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for text in texts:
                resp = await client.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self._model, "prompt": text},
                )
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"])
        return vectors


class OpenAIEmbeddingProvider:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]


EmbeddingFactory = Callable[[EmbeddingProviderConfig, str | None], Awaitable[IEmbeddingProvider]]


async def _make_ollama(cfg: EmbeddingProviderConfig, _api_key: str | None) -> IEmbeddingProvider:
    return OllamaEmbeddingProvider(model=cfg.model or "nomic-embed-text", base_url=cfg.extra.get("base_url", "http://localhost:11434"))


async def _make_openai(cfg: EmbeddingProviderConfig, api_key: str | None) -> IEmbeddingProvider:
    if not api_key:
        raise ValueError(f"embedding provider_config id={cfg.id!r} engine='openai' has no api_key_ref configured")
    return OpenAIEmbeddingProvider(api_key=api_key, model=cfg.model or "text-embedding-3-small")


_DEFAULT_REGISTRY: dict[str, EmbeddingFactory] = {
    "ollama": _make_ollama,
    "openai": _make_openai,
}


class EmbeddingProviderManager:
    def __init__(
        self,
        secret_resolver: SecretResolver,
        registry: dict[str, EmbeddingFactory] | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._registry = dict(_DEFAULT_REGISTRY) if registry is None else dict(registry)
        self._instances: dict[str, IEmbeddingProvider] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, cfg: EmbeddingProviderConfig) -> IEmbeddingProvider:
        cached = self._instances.get(cfg.id)
        if cached is not None:
            return cached

        async with self._locks[cfg.id]:
            cached = self._instances.get(cfg.id)
            if cached is not None:
                return cached

            factory = self._registry.get(cfg.engine)
            if factory is None:
                raise ValueError(f"no embedding provider factory registered for engine={cfg.engine!r}")

            api_key = await self._secret_resolver.resolve(cfg.api_key_ref) if cfg.api_key_ref else None
            instance = await factory(cfg, api_key)
            self._instances[cfg.id] = instance
            return instance
