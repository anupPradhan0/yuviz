"""EmbeddingProviderManager — cached embedding providers per provider_configs row (role='embedding').
Copies AIProviderManager's pattern; Knowledge Service must not import Conversation Service."""

from __future__ import annotations

import asyncio
import logging
from array import array
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# kb_chunks.embedding is vector(768); every provider must return exactly this width.
EMBEDDING_DIMS = 768
# Texts per provider call, and how a failed batch is retried in place (so one flaky batch does not redo the document).
EMBED_BATCH_SIZE = 32
EMBED_BATCH_RETRIES = 2
EMBED_RETRY_BASE_S = 0.5
_DEFAULT_MODELS = {"ollama": "nomic-embed-text", "openai": "text-embedding-3-small"}


class EmbeddingError(RuntimeError):
    """Transient: the provider answered, but not usably (count mismatch, NaN/inf, all-zero vector). RuntimeError, so
    the worker classifies it as retryable."""


class EmbeddingDimensionError(ValueError):
    """Permanent: the model's vectors cannot fit the vector(768) column, and retrying will not change that."""


class IEmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingProviderConfig:
    id: str
    engine: str
    model: str | None = None
    api_key_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class EmbeddingRequestRejected(ValueError):
    """Permanent: the provider answered 4xx (bad key, unknown model, malformed request). Retrying the batch cannot
    help and a misconfigured provider would otherwise be called 3x per batch, per attempt."""


# 408 (timeout), 425 and 429 (rate limit) are the 4xx codes that are worth retrying.
_PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 405, 409, 410, 411, 413, 414, 415, 422})


def effective_model(cfg: "EmbeddingProviderConfig") -> str:
    return cfg.model or _DEFAULT_MODELS.get(cfg.engine, cfg.engine)


def _validate(vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIMS:
        raise EmbeddingDimensionError(
            f"embedding provider returned {len(vector)}-dimensional vectors but kb_chunks.embedding is "
            f"vector({EMBEDDING_DIMS}); use a {EMBEDDING_DIMS}-dimension embedding model",
        )
    if not all(math.isfinite(x) for x in vector):
        raise EmbeddingError("embedding provider returned NaN or infinite values")
    if not any(vector):
        raise EmbeddingError("embedding provider returned an all-zero vector")


async def embed_in_batches(
    provider: "IEmbeddingProvider", texts: list[str], *, batch_size: int | None = None,
    retries: int | None = None, stats: dict | None = None,
) -> list[list[float]]:
    """Embed `texts` in bounded batches. A failing batch is retried alone with backoff; batches already done are
    kept. Vectors are validated (count, width, finite, non-zero); a width mismatch is permanent, the rest retry."""
    size = batch_size or EMBED_BATCH_SIZE
    attempts = (EMBED_BATCH_RETRIES if retries is None else retries) + 1
    vectors: list[list[float]] = []
    retried = 0
    for start in range(0, len(texts), size):
        batch = texts[start:start + size]
        for attempt in range(1, attempts + 1):
            try:
                got = await provider.embed(batch)
                if len(got) != len(batch):
                    raise EmbeddingError(f"embedding provider returned {len(got)} vectors for {len(batch)} texts")
                for v in got:
                    _validate(v)
                # float32 arrays (what pgvector stores anyway): 3 KB per vector instead of ~25 KB of Python floats, so
                # holding a document's vectors is bounded by chunks x 768 x 4 bytes (5,000 chunks = 15 MB).
                vectors.extend(array("f", v) for v in got)
                break
            except EmbeddingDimensionError:
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _PERMANENT_STATUS:
                    raise EmbeddingRequestRejected(f"embedding provider rejected the request (HTTP {status})") from None
                if attempt == attempts:
                    raise
                retried += 1
                await asyncio.sleep(EMBED_RETRY_BASE_S * (2 ** (attempt - 1)))
            except Exception as exc:
                if attempt == attempts:
                    raise
                retried += 1
                log.warning("embedding batch at %d failed (attempt %d/%d): %s", start, attempt, attempts, type(exc).__name__)
                await asyncio.sleep(EMBED_RETRY_BASE_S * (2 ** (attempt - 1)))
    if stats is not None:
        stats.update(batches=-(-len(texts) // size), batch_retries=retried)
    return vectors


class OllamaEmbeddingProvider:
    def __init__(self, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434") -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # /api/embeddings has no batch form: one request per text.
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
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        # Only text-embedding-3 supports `dimensions` (ada-002 400s and can't fit the column anyway).
        if self._model.startswith("text-embedding-3"):
            payload["dimensions"] = EMBEDDING_DIMS
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
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
