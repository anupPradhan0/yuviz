"""Hosted rerankers behind retrieval's IReranker hook (Cohere v2 rerank, Voyage rerank).

OFF unless an operator sets KNOWLEDGE_RERANKER=cohere|voyage; even then only an agent whose retrieval policy has
rerank=true reaches it (a request can turn rerank off for a call, never on). Privacy: when on, the caller's query and
the TEXT of up to KNOWLEDGE_RERANK_TOP_N candidate chunks (each cut to KNOWLEDGE_RERANK_DOC_CHARS, all together to
KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS) leave the platform for the provider. Nothing else is sent (no ids, no tenant or
document names), and no query or chunk text is logged.

The provider key is PLATFORM-WIDE: one key, one quota, shared by every tenant whose agent has the flag. Per-tenant
call budgets (TenantBudget) are what stop one tenant spending everyone's quota, and quota 429s and budget refusals
do not open the shared breaker (only timeouts, 5xx and transport errors do).

Failure never costs the caller: a timeout, 429, 5xx, malformed or wrong-sized answer raises RerankError, which
retrieval turns into the fused order plus degraded ['rerank_skipped', <reason>]. No retries: the stage budget
(120 ms) has no room for one."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import ops_metrics
from .envparse import env_float, env_int
from .secret_resolver import CompositeSecretResolver, SecretResolver
from .vector_repository import VectorMatch

log = logging.getLogger(__name__)


class RerankError(RuntimeError):
    """The provider could not be used for this call. `degraded_reason` is a fixed code (never provider text).
    `breaker_failure=False` for outcomes that say nothing about the provider's health (a quota 429, our own budget)."""

    def __init__(self, degraded_reason: str, *, breaker_failure: bool = True) -> None:
        super().__init__(degraded_reason)
        self.degraded_reason = degraded_reason
        self.breaker_failure = breaker_failure


@dataclass(frozen=True)
class RerankConfig:
    top_n: int = 20                  # candidates sent; the rest keep their fused order behind them
    doc_chars: int = 1200            # per-candidate text cap
    max_payload_chars: int = 20000   # all candidate text together
    http_timeout_s: float = 0.095    # under the 120 ms stage budget, so the adapter fails before the stage cuts it
    # Decisive: the top hit has dense support with cosine >= decisive_score and leads the BEST OTHER dense candidate
    # by >= decisive_gap. Reordering a clear winner is unlikely to help and costs a provider call.
    decisive_score: float = 0.85
    decisive_gap: float = 0.10
    # Per-tenant budget on provider calls (the key is platform-wide: this is what keeps one tenant from spending
    # everyone's quota). Token bucket: `tenant_rate` calls/s sustained, `tenant_burst` at once.
    tenant_rate: float = 0.625       # derived, see from_env: platform_rate x tenant_share / processes
    tenant_burst: int = 5
    # Sizing: the vendor quota (platform_rate calls/s for the one platform key) is split across `processes` knowledge
    # service processes, and one tenant may use `tenant_share` of it. The whole process is also capped at its share
    # (platform_rate / processes), so no mix of tenants can push the shared key into 429 for everyone.
    platform_rate: float = 10.0
    processes: int = 4
    tenant_share: float = 0.25

    @classmethod
    def from_env(cls) -> "RerankConfig":
        d = cls()
        return cls(
            top_n=env_int("KNOWLEDGE_RERANK_TOP_N", d.top_n, lo=2, hi=200),
            doc_chars=env_int("KNOWLEDGE_RERANK_DOC_CHARS", d.doc_chars, lo=100, hi=20000),
            max_payload_chars=env_int("KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS", d.max_payload_chars, lo=1000, hi=200000),
            http_timeout_s=env_float("KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS", d.http_timeout_s * 1000, lo=5, hi=1000) / 1000,
            decisive_score=env_float("KNOWLEDGE_RERANK_DECISIVE_SCORE", d.decisive_score, lo=0, hi=1),
            decisive_gap=env_float("KNOWLEDGE_RERANK_DECISIVE_GAP", d.decisive_gap, lo=0, hi=1),
            tenant_rate=_derived_tenant_rate(d),
            tenant_burst=env_int("KNOWLEDGE_RERANK_TENANT_BURST", d.tenant_burst, lo=1, hi=1000),
            platform_rate=env_float("KNOWLEDGE_RERANK_PLATFORM_RATE", d.platform_rate, lo=0.1, hi=100000),
            processes=env_int("KNOWLEDGE_RERANK_PROCESSES", d.processes, lo=1, hi=10000),
            tenant_share=env_float("KNOWLEDGE_RERANK_TENANT_SHARE", d.tenant_share, lo=0.01, hi=1),
        )

    @property
    def process_rate(self) -> float:
        return self.platform_rate / self.processes


def _derived_tenant_rate(d: "RerankConfig") -> float:
    """KNOWLEDGE_RERANK_TENANT_RATE wins when it is a usable number; else platform quota x one tenant's share /
    processes (so a garbage value falls back to the derived rate, not to a constant)."""
    platform = env_float("KNOWLEDGE_RERANK_PLATFORM_RATE", d.platform_rate, lo=0.1, hi=100000)
    processes = env_int("KNOWLEDGE_RERANK_PROCESSES", d.processes, lo=1, hi=10000)
    share = env_float("KNOWLEDGE_RERANK_TENANT_SHARE", d.tenant_share, lo=0.01, hi=1)
    derived = max(platform * share / processes, 0.01)
    if os.environ.get("KNOWLEDGE_RERANK_TENANT_RATE", "").strip():
        return env_float("KNOWLEDGE_RERANK_TENANT_RATE", derived, lo=0.01, hi=1000)
    return derived


class TenantBudget:
    """Per-tenant token buckets for provider calls. In-process (each worker process has its own budget, so the
    effective ceiling is rate x processes); bounded memory."""

    def __init__(self, rate: float, burst: int, clock=time.monotonic, max_tenants: int = 10000) -> None:
        self._rate, self._burst, self._clock, self._max = rate, float(burst), clock, max_tenants
        self._buckets: dict[str, tuple[float, float]] = {}  # tenant id -> (tokens, last refill)

    def allow(self, tenant_id: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(tenant_id, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        ok = tokens >= 1.0
        self._buckets[tenant_id] = (tokens - 1.0 if ok else tokens, now)
        if len(self._buckets) > self._max:  # forget the idlest half: a full bucket is the default anyway
            for stale in sorted(self._buckets, key=lambda k: self._buckets[k][1])[: self._max // 2]:
                del self._buckets[stale]
        return ok


def is_decisive(matches: list[VectorMatch], cfg: RerankConfig) -> bool:
    """True when the fused winner is clear enough that reranking is skipped (documented in docs/knowledge-phase7.md).
    Compared on the DENSE cosine only (a lexical score is another scale): the top hit must have dense support and
    lead the best OTHER dense candidate anywhere in the list, not just the next item in fused order."""
    if not matches:
        return True
    top = matches[0]
    if "dense" not in top.found_by or top.score < cfg.decisive_score:
        return False
    others = [m.score for m in matches[1:] if "dense" in m.found_by]
    return not others or (top.score - max(others)) >= cfg.decisive_gap


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
        transport: httpx.AsyncBaseTransport | None = None, allowed_hosts: frozenset[str] | None = None,
        allow_insecure_for_tests: bool = False, verify: Any = True,
    ) -> None:
        self._model = model or self.default_model
        self._url = url or self.default_url
        self._allow_insecure = allow_insecure_for_tests
        validate_url(self._url, allowed_hosts or default_hosts(self.default_url), allow_insecure_for_tests)
        self._key_ref = key_ref or self.default_key_ref
        self.config = config or RerankConfig.from_env()
        self._resolver = resolver or CompositeSecretResolver()
        self._key: str | None = None
        self.budget = TenantBudget(self.config.tenant_rate, self.config.tenant_burst)
        # The whole process's share of the platform quota, across every tenant.
        self.process_budget = TenantBudget(self.config.process_rate, max(10, int(self.config.process_rate * 2)))
        # One long-lived client: connections are kept alive, so the 95 ms budget is not spent on a TLS handshake.
        self._client = httpx.AsyncClient(
            transport=transport, timeout=self.config.http_timeout_s, verify=verify,
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120.0))

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

    async def warm(self) -> None:
        """Open the connection ahead of the first call (TLS handshake, DNS). Sends no key, query or text: a HEAD to
        the endpoint's origin. Best effort."""
        parts = urlsplit(self._url)
        try:
            await self._client.head(f"{parts.scheme}://{parts.netloc}/", timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            log.info("reranker warm-up failed (%s); the first call pays the connection", type(exc).__name__)

    budgeted = True  # retrieval passes the tenant so the per-tenant budget applies

    async def rerank(self, query: str, matches: list[VectorMatch], tenant_id: str | None = None) -> list[VectorMatch]:
        cfg = self.config
        if len(matches) < 2:
            return matches
        if is_decisive(matches, cfg):
            ops_metrics.inc("rerank_decisive_skip")
            return matches
        sent, docs = _documents(matches, cfg)
        if len(sent) < 2:
            return matches
        if tenant_id is not None and not self.budget.allow(tenant_id):
            raise RerankError("rerank_budget", breaker_failure=False)  # the key is platform-wide: cap each tenant
        if tenant_id is not None and not self.process_budget.allow("*"):
            raise RerankError("rerank_budget", breaker_failure=False)  # and the process as a whole
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
            raise RerankError("rerank_rate_limited", breaker_failure=False)  # a quota, not an unhealthy provider
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


def default_hosts(url: str) -> frozenset[str]:
    return frozenset({urlsplit(url).hostname or ""})


def validate_url(url: str, allowed_hosts: frozenset[str], allow_insecure: bool = False) -> None:
    """The platform key goes in an Authorization header: https only, and only to a host the operator allowed (the
    vendor's by default). Raises ValueError, which refuses to start the service rather than send a key elsewhere."""
    parts = urlsplit(url)
    if not allow_insecure and parts.scheme != "https":
        raise ValueError("KNOWLEDGE_RERANK_URL must be an https URL: the provider key is sent in a header")
    if not parts.hostname or (not allow_insecure and parts.hostname not in allowed_hosts):
        raise ValueError(
            f"KNOWLEDGE_RERANK_URL host {parts.hostname!r} is not allowed; set KNOWLEDGE_RERANK_ALLOWED_HOSTS to "
            "list it explicitly")
    if parts.username or parts.password:
        raise ValueError("KNOWLEDGE_RERANK_URL must not contain credentials")


def build_reranker(env: dict[str, str] | None = None) -> HostedReranker | None:
    """KNOWLEDGE_RERANKER = none (default) | cohere | voyage. An unknown value is logged and treated as none. A
    KNOWLEDGE_RERANK_URL that is not https, or whose host is not allowed, raises ValueError."""
    env = os.environ if env is None else env
    choice = (env.get("KNOWLEDGE_RERANKER") or "none").strip().lower()
    classes = {"cohere": CohereReranker, "voyage": VoyageReranker}
    if choice in ("", "none"):
        return None
    cls = classes.get(choice)
    if cls is None:
        log.warning("KNOWLEDGE_RERANKER=%r is not one of none|cohere|voyage; reranking stays off", choice[:20])
        return None
    extra = frozenset(h.strip() for h in (env.get("KNOWLEDGE_RERANK_ALLOWED_HOSTS") or "").split(",") if h.strip())
    return cls(model=env.get("KNOWLEDGE_RERANK_MODEL") or None, url=env.get("KNOWLEDGE_RERANK_URL") or None,
               key_ref=env.get("KNOWLEDGE_RERANK_API_KEY_REF") or None,
               allowed_hosts=(default_hosts(cls.default_url) | extra))
