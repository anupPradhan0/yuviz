"""Retrieval time budget and per-dependency circuit breakers.

The numbers are the hot-path budget from the RAG redesign (#87): each stage has its own timeout and the whole
call has a hard deadline, so a slow embedder or database can never hold a live call. Every value is overridable
from the environment; RetrievalConfig is frozen so a request cannot see it change mid-flight."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


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

    def __init__(self, failures: int, cooldown_s: float, clock=time.monotonic) -> None:
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
            return True
        return False

    def record_success(self) -> None:
        self._consecutive = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._consecutive += 1
        if self._probing or self._consecutive >= self._threshold:
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
            breaker = CircuitBreaker(config.breaker_failures, config.breaker_cooldown_s)
            self._breakers[key] = breaker
        return breaker

    def clear(self) -> None:
        self._breakers.clear()


breakers = BreakerRegistry()
