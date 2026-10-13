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

from libs.egress_guard import UnsafeProviderUrl, failure_code, guarded_client, validate_base_url

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# kb_chunks.embedding is vector(768); every provider must return exactly this width.
EMBEDDING_DIMS = 768
# Texts per provider call, and how a failed batch is retried in place (so one flaky batch does not redo the document).
EMBED_BATCH_SIZE = 32
EMBED_BATCH_RETRIES = 2
EMBED_RETRY_BASE_S = 0.5
_DEFAULT_MODELS = {"ollama": "nomic-embed-text", "openai": "text-embedding-3-small"}


class ProviderError(Exception):
    """A provider failure reduced to a FIXED code. `str(exc)` is the code and nothing else: the exception text of a
    request to a tenant-chosen URL (status line, body, host, port, path) must never reach a stored error, because the
    tenant can read it back and would otherwise get a port scanner. Details go to the server log only."""

    code = "provider_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class ProviderTransientError(ProviderError, RuntimeError):
    """Worth retrying (unreachable, timeout, 408/429/5xx). RuntimeError, so the worker classifies it as retryable."""


class ProviderPermanentError(ProviderError, ValueError):
    """Retrying cannot help (auth, unknown model, malformed request, unusable URL). ValueError, so the worker fails it."""


class EmbeddingError(ProviderTransientError):
    """Transient: the provider answered, but not usably (count mismatch, NaN/inf, all-zero vector, invalid JSON)."""

    code = "provider_bad_response"


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


class EmbeddingRequestRejected(ProviderPermanentError):
    """Permanent: the provider answered 4xx (bad key, unknown model, malformed request). Retrying the batch cannot
    help and a misconfigured provider would otherwise be called 3x per batch, per attempt."""


class ProviderUrlRejected(ProviderPermanentError):
    """The config's base_url is not acceptable (see libs/egress_guard). The tenant typed it, so this code is not an oracle."""

    code = "provider_url_invalid"



def provider_error(exc: BaseException) -> ProviderError | None:
    """Map what an HTTP provider call can raise to a fixed-code ProviderError (None: not a provider failure).

    Every connection-level failure (refused, timeout, DNS, TLS, egress refusal) is ONE code, so the stored error cannot
    tell a tenant whether a port is closed, filtered, or open. Only 401/403 are named, because the owner must fix the key."""
    if isinstance(exc, ProviderError):
        return exc
    failure = failure_code(exc)
    if failure is None:
        return None
    code, permanent = failure
    return EmbeddingRequestRejected(code) if permanent else ProviderTransientError(code)


def effective_model(cfg: "EmbeddingProviderConfig") -> str:
    return cfg.model or _DEFAULT_MODELS.get(cfg.engine, cfg.engine)


def _validate(vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIMS:
        raise EmbeddingDimensionError(
            f"embedding provider returned {len(vector)}-dimensional vectors but kb_chunks.embedding is "
            f"vector({EMBEDDING_DIMS}); use a {EMBEDDING_DIMS}-dimension embedding model",
        )
    if not all(math.isfinite(x) for x in vector):
        log.warning("embedding provider returned NaN or infinite values")
        raise EmbeddingError()
    if not any(vector):
        log.warning("embedding provider returned an all-zero vector")
        raise EmbeddingError()


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
                    log.warning("embedding provider returned %d vectors for %d texts", len(got), len(batch))
                    raise EmbeddingError()
                for v in got:
                    _validate(v)
                # float32 arrays (what pgvector stores anyway): 3 KB per vector instead of ~25 KB of Python floats, so
                # holding a document's vectors is bounded by chunks x 768 x 4 bytes (5,000 chunks = 15 MB).
                vectors.extend(array("f", v) for v in got)
                break
            except EmbeddingDimensionError:
                raise
            except Exception as exc:
                converted = provider_error(exc)
                if isinstance(converted, ProviderPermanentError):
                    raise converted from None
                if attempt == attempts:
                    raise (converted or exc) from None
                retried += 1
                log.warning("embedding batch at %d failed (attempt %d/%d): %s", start, attempt, attempts,
                            converted.code if converted else type(exc).__name__)
                await asyncio.sleep(EMBED_RETRY_BASE_S * (2 ** (attempt - 1)))
    if stats is not None:
        stats.update(batches=-(-len(texts) // size), batch_retries=retried)
    return vectors


class OllamaEmbeddingProvider:
    def __init__(self, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434") -> None:
        # The tenant owns base_url, so only its origin is used; the API path is ours (libs/egress_guard).
        try:
            self._origin = validate_base_url(base_url)
        except UnsafeProviderUrl:
            raise ProviderUrlRejected() from None
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # /api/embeddings has no batch form: one request per text.
        vectors: list[list[float]] = []
        try:
            async with guarded_client(timeout=30.0) as client:
                for text in texts:
                    resp = await client.post(
                        f"{self._origin.url}/api/embeddings",
                        json={"model": self._model, "prompt": text},
                    )
                    resp.raise_for_status()
                    vectors.append(_embedding_of(resp))
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            converted = provider_error(exc)
            if converted is None:
                raise
            raise converted from None
        return vectors


def _embedding_of(resp: httpx.Response) -> list[float]:
    try:
        vector = resp.json()["embedding"]
    except (ValueError, KeyError, TypeError):
        raise EmbeddingError() from None
    if not isinstance(vector, list):
        raise EmbeddingError()
    return vector


class OpenAIEmbeddingProvider:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        # Only text-embedding-3 supports `dimensions` (ada-002 400s and can't fit the column anyway).
        if self._model.startswith("text-embedding-3"):
            payload["dimensions"] = EMBEDDING_DIMS
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                try:
                    data = resp.json()["data"]
                    return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]
                except (ValueError, KeyError, TypeError):
                    raise EmbeddingError() from None
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            converted = provider_error(exc)
            if converted is None:
                raise
            raise converted from None


EmbeddingFactory = Callable[[EmbeddingProviderConfig, str | None], Awaitable[IEmbeddingProvider]]


async def _make_ollama(cfg: EmbeddingProviderConfig, _api_key: str | None) -> IEmbeddingProvider:
    return OllamaEmbeddingProvider(
        model=cfg.model or "nomic-embed-text", base_url=cfg.extra.get("base_url") or "http://localhost:11434")


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
