"""Retrieval time budget and per-dependency circuit breakers.

The numbers are the hot-path budget from the RAG redesign (#87): each stage has its own timeout and the whole
call has a hard deadline, so a slow embedder or database can never hold a live call. Every value is overridable
from the environment; RetrievalConfig is frozen so a request cannot see it change mid-flight."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RetrievalConfig:
    embed_timeout_s: float = 0.150
    dense_timeout_s: float = 0.120
    lexical_timeout_s: float = 0.120
    rerank_timeout_s: float = 0.120
    deadline_s: float = 0.400
    # Breaker: consecutive failures before opening, then seconds before one half-open probe.
    breaker_failures: int = 5
    breaker_cooldown_s: float = 20.0
    # Close scores: top1 below `low_confidence_top` AND top1-top2 below `low_confidence_gap` => low_confidence.
    low_confidence_top: float = 0.5
    low_confidence_gap: float = 0.02
    # ts_rank_cd(...,32) is rank/(rank+1): one matching term in a ~12-word chunk scores ~0.09, so 0.05 keeps
    # single-term hits (SKUs, names) and only drops the near-zero ones.
    lexical_min_score: float = 0.05
    # A top hit supported only by lexical evidence is low_confidence unless it scores at least this much
    # (about three matching terms in a 12-word chunk). minimum_score applies to dense similarity only.
    lexical_confident_score: float = 0.3
    # Admission: in-flight retrievals per process, and per tenant. A retrieval holds up to 3 pool connections
    # when the caller keeps one, so the default is (pool_max - 4) // 3, well below pool_max / 2.
    max_inflight: int = 5
    tenant_max_inflight: int = 3
    queue_wait_s: float = 0.150
    acquire_timeout_s: float = 0.060  # waiting longer than this for a pool connection means overload
    max_query_chars: int = 1000

    @classmethod
    def from_env(cls) -> "RetrievalConfig":
        d = cls()
        return cls(
            embed_timeout_s=_env_float("KNOWLEDGE_EMBED_TIMEOUT_MS", d.embed_timeout_s * 1000) / 1000,
            dense_timeout_s=_env_float("KNOWLEDGE_DENSE_TIMEOUT_MS", d.dense_timeout_s * 1000) / 1000,
            lexical_timeout_s=_env_float("KNOWLEDGE_LEXICAL_TIMEOUT_MS", d.lexical_timeout_s * 1000) / 1000,
            rerank_timeout_s=_env_float("KNOWLEDGE_RERANK_TIMEOUT_MS", d.rerank_timeout_s * 1000) / 1000,
            deadline_s=_env_float("KNOWLEDGE_RETRIEVAL_DEADLINE_MS", d.deadline_s * 1000) / 1000,
            breaker_failures=int(_env_float("KNOWLEDGE_BREAKER_FAILURES", d.breaker_failures)),
            breaker_cooldown_s=_env_float("KNOWLEDGE_BREAKER_COOLDOWN_S", d.breaker_cooldown_s),
            low_confidence_top=_env_float("KNOWLEDGE_LOW_CONFIDENCE_TOP", d.low_confidence_top),
            low_confidence_gap=_env_float("KNOWLEDGE_LOW_CONFIDENCE_GAP", d.low_confidence_gap),
            lexical_min_score=_env_float("KNOWLEDGE_LEXICAL_MIN_SCORE", d.lexical_min_score),
            lexical_confident_score=_env_float("KNOWLEDGE_LEXICAL_CONFIDENT_SCORE", d.lexical_confident_score),
            max_inflight=int(_env_float(
                "KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT", max(2, (_env_float("KNOWLEDGE_DB_POOL_MAX", 20) - 4) // 3))),
            tenant_max_inflight=int(_env_float("KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT", d.tenant_max_inflight)),
            queue_wait_s=_env_float("KNOWLEDGE_RETRIEVAL_QUEUE_WAIT_MS", d.queue_wait_s * 1000) / 1000,
            acquire_timeout_s=_env_float("KNOWLEDGE_POOL_ACQUIRE_TIMEOUT_MS", d.acquire_timeout_s * 1000) / 1000,
            max_query_chars=int(_env_float("KNOWLEDGE_RETRIEVAL_MAX_QUERY_CHARS", d.max_query_chars)),
        )


class RetrievalUnavailable(RuntimeError):
    """Every retrieval branch failed or timed out and there is no prompt-mode content to fall back on.
    The router maps this to status='unavailable'; `degraded` and `trace_id` ride along for the response."""

    def __init__(self, message: str, *, degraded: list[str], trace_id: str) -> None:
        super().__init__(message)
        self.degraded = degraded
        self.trace_id = trace_id


class CircuitBreaker:
    """closed -> open after `failures` consecutive failures -> one half-open probe after `cooldown_s`.
    While open, allow() is a clock read: a dead dependency costs the hot path nothing."""

    def __init__(self, failures: int, cooldown_s: float, clock=time.monotonic, name: str = "") -> None:
        self.name = name  # an id (embedding_config_id) or "rerank": never tenant data or query text
        self._threshold = failures
        self._cooldown = cooldown_s
        self._clock = clock
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            self._probing = True  # exactly one caller probes; the rest keep failing fast
            log.warning("circuit breaker half-open dependency=%s", self.name)
            return True
        return False

    def record_success(self) -> None:
        if self._opened_at is not None:
            log.warning("circuit breaker closed dependency=%s", self.name)
        self._consecutive = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._consecutive += 1
        if self._probing or self._consecutive >= self._threshold:
            if self._opened_at is None or self._probing:
                log.warning("circuit breaker opened dependency=%s consecutive_failures=%d", self.name, self._consecutive)
            self._opened_at = self._clock()
            self._probing = False

    def release_probe(self) -> None:
        """A cancelled probe says nothing about the dependency; let the next caller probe."""
        self._probing = False


class BreakerRegistry:
    """Breakers by key. Embedder keys carry the (tenant-owned) embedding_config_id, so one tenant's
    broken provider never opens the breaker for another tenant."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, key: str, config: RetrievalConfig) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(config.breaker_failures, config.breaker_cooldown_s, name=key)
            self._breakers[key] = breaker
        return breaker

    def clear(self) -> None:
        self._breakers.clear()


breakers = BreakerRegistry()


class PoolOverloaded(RuntimeError):
    """No pooled connection became free within acquire_timeout_s: load, not a broken dependency."""


class BoundedPool:
    """Pool facade for tenant_conn(): acquire() gives up after `timeout_s` and raises PoolOverloaded, so a
    saturated pool is reported as overload instead of surfacing as a branch that "timed out"."""

    def __init__(self, pool: Any, timeout_s: float) -> None:
        self._pool = pool
        self._timeout_s = timeout_s

    def acquire(self) -> "_BoundedAcquire":
        return _BoundedAcquire(self._pool, self._timeout_s)


class _BoundedAcquire:
    def __init__(self, pool: Any, timeout_s: float) -> None:
        self._cm = pool.acquire(timeout=timeout_s)

    async def __aenter__(self) -> Any:
        try:
            return await self._cm.__aenter__()
        except asyncio.TimeoutError as exc:
            raise PoolOverloaded("database pool exhausted") from exc

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._cm.__aexit__(*exc_info)


@dataclass
class _Waiter:
    key: str
    max_inflight: int
    tenant_max: int
    fut: "asyncio.Future[None]" = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class AdmissionLimiter:
    """In-process cap on concurrent retrievals, overall and per tenant, FIFO with a short bounded wait.
    It protects the DB pool: past the cap a caller gets a typed 'overloaded' answer quickly instead of every
    request fighting for connections and timing out."""

    def __init__(self) -> None:
        self._total = 0
        self._per: dict[str, int] = {}
        self._waiters: list[_Waiter] = []

    def _fits(self, key: str, max_inflight: int, tenant_max: int) -> bool:
        return self._total < max_inflight and self._per.get(key, 0) < tenant_max

    def _take(self, key: str) -> None:
        self._total += 1
        self._per[key] = self._per.get(key, 0) + 1

    async def acquire(self, key: str, cfg: RetrievalConfig, wait_s: float) -> bool:
        if not self._waiters and self._fits(key, cfg.max_inflight, cfg.tenant_max_inflight):
            self._take(key)
            return True
        if wait_s <= 0:
            return False
        waiter = _Waiter(key, cfg.max_inflight, cfg.tenant_max_inflight)
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter.fut), timeout=wait_s)
            return True
        except asyncio.TimeoutError:
            if waiter.fut.done() and not waiter.fut.cancelled():  # a slot was handed over as we timed out
                self.release(key)
            return False
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            if not waiter.fut.done():
                waiter.fut.cancel()

    def release(self, key: str) -> None:
        self._total -= 1
        left = self._per.get(key, 1) - 1
        if left <= 0:
            self._per.pop(key, None)
        else:
            self._per[key] = left
        for waiter in list(self._waiters):
            if waiter.fut.done():
                self._waiters.remove(waiter)
            elif self._fits(waiter.key, waiter.max_inflight, waiter.tenant_max):
                self._waiters.remove(waiter)
                self._take(waiter.key)
                waiter.fut.set_result(None)

    @property
    def in_flight(self) -> int:
        return self._total


admission = AdmissionLimiter()
