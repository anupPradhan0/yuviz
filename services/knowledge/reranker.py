"""Hosted rerankers behind retrieval's IReranker hook (Cohere v2 rerank, Voyage rerank).

OFF unless an operator sets KNOWLEDGE_RERANKER=cohere|voyage; even then an agent's policy flag `rerank` must be true
for a call to reach it. Privacy: when on, the caller's query and the TEXT of up to KNOWLEDGE_RERANK_TOP_N candidate
chunks (each cut to KNOWLEDGE_RERANK_DOC_CHARS, all together to KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS) leave the platform
for the provider. Nothing else is sent (no ids, no tenant or document names), and no query or chunk text is logged.
The provider key is platform-level (an env/secret ref), never a tenant's.

Failure never costs the caller: a timeout, 429, 5xx, malformed or wrong-sized answer raises RerankError, which
retrieval turns into the fused order plus degraded ['rerank_skipped', <reason>] and a breaker failure. No retries:
the stage budget (120 ms) has no room for one."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

import httpx

from . import ops_metrics
from .secret_resolver import CompositeSecretResolver, SecretResolver
from .vector_repository import VectorMatch

log = logging.getLogger(__name__)


class RerankError(RuntimeError):
    """The provider could not be used for this call. `degraded_reason` is a fixed code (never provider text)."""

    def __init__(self, degraded_reason: str) -> None:
        super().__init__(degraded_reason)
        self.degraded_reason = degraded_reason


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class RerankConfig:
    top_n: int = 20                  # candidates sent; the rest keep their fused order behind them
    doc_chars: int = 1200            # per-candidate text cap
    max_payload_chars: int = 20000   # all candidate text together
    http_timeout_s: float = 0.095    # under the 120 ms stage budget, so the adapter fails before the stage cuts it
    # Decisive: the top hit has dense AND lexical support, a dense cosine >= decisive_score, and leads the runner-up
    # by >= decisive_gap. Reranking cannot plausibly change a clear winner, so the provider call is saved.
    decisive_score: float = 0.85
    decisive_gap: float = 0.10

    @classmethod
    def from_env(cls) -> "RerankConfig":
        d = cls()
        return cls(
            top_n=max(2, int(_env_float("KNOWLEDGE_RERANK_TOP_N", d.top_n))),
            doc_chars=max(100, int(_env_float("KNOWLEDGE_RERANK_DOC_CHARS", d.doc_chars))),
            max_payload_chars=max(1000, int(_env_float("KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS", d.max_payload_chars))),
            http_timeout_s=_env_float("KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS", d.http_timeout_s * 1000) / 1000,
            decisive_score=_env_float("KNOWLEDGE_RERANK_DECISIVE_SCORE", d.decisive_score),
            decisive_gap=_env_float("KNOWLEDGE_RERANK_DECISIVE_GAP", d.decisive_gap),
        )


def is_decisive(matches: list[VectorMatch], cfg: RerankConfig) -> bool:
    """True when the fused winner is clear enough that reranking is skipped (documented in docs/knowledge-phase7.md).
    Only a hit found by both branches can be decisive; a lone hit with that support counts as decisive too."""
    if not matches:
        return True
    top = matches[0]
    if "dense" not in top.found_by or "lexical" not in top.found_by or top.score < cfg.decisive_score:
        return False
    second = matches[1] if len(matches) > 1 else None
    return second is None or (top.score - second.score) >= cfg.decisive_gap


def _documents(candidates: list[VectorMatch], cfg: RerankConfig) -> tuple[list[VectorMatch], list[str]]:
    """Chunk text only, capped per document and in total; candidates past the payload cap are not sent."""
    sent: list[VectorMatch] = []
    docs: list[str] = []
    used = 0
    for m in candidates[: cfg.top_n]:
        text = m.content[: cfg.doc_chars]
        if sent and used + len(text) > cfg.max_payload_chars:
            break
        sent.append(m)
        docs.append(text or " ")
        used += len(text)
    return sent, docs


class HostedReranker:
    """Shared flow; subclasses say how the request and response look. `transport` is for tests (httpx.MockTransport)."""

    configured = True
    name = ""
    default_url = ""
    default_model = ""
    default_key_ref = ""

    def __init__(
        self, *, model: str | None = None, url: str | None = None, key_ref: str | None = None,
        config: RerankConfig | None = None, resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model or self.default_model
        self._url = url or self.default_url
        self._key_ref = key_ref or self.default_key_ref
        self.config = config or RerankConfig.from_env()
        self._resolver = resolver or CompositeSecretResolver()
        self._key: str | None = None
        self._client = httpx.AsyncClient(transport=transport, timeout=self.config.http_timeout_s)

    # -- provider specifics
    def _request(self, query: str, docs: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    def _results(self, body: Any) -> list[Any]:
        raise NotImplementedError

    async def _api_key(self) -> str:
        if self._key is None:
            try:
                self._key = await self._resolver.resolve(self._key_ref)
            except Exception as exc:  # noqa: BLE001 - the ref may hold a pasted key: never echo it
                log.warning("reranker %s: api key ref could not be resolved (%s)", self.name, type(exc).__name__)
                raise RerankError("rerank_not_configured") from None
        return self._key

    async def rerank(self, query: str, matches: list[VectorMatch]) -> list[VectorMatch]:
        cfg = self.config
        if len(matches) < 2:
            return matches
        if is_decisive(matches, cfg):
            ops_metrics.inc("rerank_decisive_skip")
            return matches
        sent, docs = _documents(matches, cfg)
        if len(sent) < 2:
            return matches
        key = await self._api_key()
        try:
            async with asyncio.timeout(cfg.http_timeout_s):
                resp = await self._client.post(
                    self._url, headers={"Authorization": f"Bearer {key}"}, json=self._request(query, docs))
        except (TimeoutError, httpx.TimeoutException):
            raise RerankError("rerank_timeout") from None
        except httpx.HTTPError:
            raise RerankError("rerank_provider_error") from None
        if resp.status_code == 429:
            raise RerankError("rerank_rate_limited")
        if resp.status_code != 200:
            raise RerankError("rerank_provider_error")
        try:
            order = self._parse(resp.json(), len(sent))
        except (ValueError, TypeError, KeyError):
            raise RerankError("rerank_bad_response") from None
        ops_metrics.inc("rerank_calls")
        return [sent[i] for i in order] + matches[len(sent):]

    def _parse(self, body: Any, n: int) -> list[int]:
        """Best first, covering every sent candidate exactly once; anything else is a bad response."""
        rows = self._results(body)
        if not isinstance(rows, list) or len(rows) != n:
            raise ValueError("wrong result count")
        scored: list[tuple[float, int]] = []
        for row in rows:
            idx, score = row["index"], row["relevance_score"]
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n:
                raise ValueError("bad index")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ValueError("bad score")
            scored.append((float(score), idx))
        if len({i for _, i in scored}) != n:
            raise ValueError("duplicate index")
        scored.sort(key=lambda p: (-p[0], p[1]))  # ties keep the fused order
        return [i for _, i in scored]

    async def aclose(self) -> None:
        await self._client.aclose()


class CohereReranker(HostedReranker):
    name = "cohere"
    default_url = "https://api.cohere.com/v2/rerank"
    default_model = "rerank-v3.5"
    default_key_ref = "env:COHERE_API_KEY"

    def _request(self, query: str, docs: list[str]) -> dict[str, Any]:
        return {"model": self._model, "query": query, "documents": docs, "top_n": len(docs)}

    def _results(self, body: Any) -> list[Any]:
        return body["results"]


class VoyageReranker(HostedReranker):
    name = "voyage"
    default_url = "https://api.voyageai.com/v1/rerank"
    default_model = "rerank-2"
    default_key_ref = "env:VOYAGE_API_KEY"

    def _request(self, query: str, docs: list[str]) -> dict[str, Any]:
        return {"model": self._model, "query": query, "documents": docs, "top_k": len(docs)}

    def _results(self, body: Any) -> list[Any]:
        return body["data"]


def build_reranker(env: dict[str, str] | None = None) -> HostedReranker | None:
    """KNOWLEDGE_RERANKER = none (default) | cohere | voyage. An unknown value is logged and treated as none."""
    env = os.environ if env is None else env
    choice = (env.get("KNOWLEDGE_RERANKER") or "none").strip().lower()
    classes = {"cohere": CohereReranker, "voyage": VoyageReranker}
    if choice in ("", "none"):
        return None
    cls = classes.get(choice)
    if cls is None:
        log.warning("KNOWLEDGE_RERANKER=%r is not one of none|cohere|voyage; reranking stays off", choice[:20])
        return None
    return cls(model=env.get("KNOWLEDGE_RERANK_MODEL") or None, url=env.get("KNOWLEDGE_RERANK_URL") or None,
               key_ref=env.get("KNOWLEDGE_RERANK_API_KEY_REF") or None)
