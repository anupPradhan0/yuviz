"""
IMetrics — minimal counter/histogram interface for the Conversation Service.

Mirrors the C++ gateway's IMetrics (increment/observe — see
gateway/include/metrics/IMetrics.h) rather than inventing a new shape: no
Python-side metrics abstraction existed before Phase 5C of AI-to-human
transfer, and this is the smallest thing that lets pipeline.py/session.py
emit named counters without depending on a specific backend (Prometheus,
StatsD, or just logging) here.

NullMetrics is the default everywhere — emitting metrics is opt-in, exactly
like TranscriptBuilder's pool=None posture, so nothing breaks or slows down
for a caller that doesn't wire a real sink.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class IMetrics(Protocol):
    def increment(self, name: str, value: float = 1.0) -> None: ...
    def observe(self, name: str, value: float) -> None: ...


class NullMetrics:
    """Default sink — every call is a no-op."""

    def increment(self, name: str, value: float = 1.0) -> None:
        pass

    def observe(self, name: str, value: float) -> None:
        pass


class LoggingMetrics:
    """Dev/debug sink — logs every emission instead of dropping it. Not
    wired anywhere by default; useful for local runs without a real metrics
    backend configured."""

    def increment(self, name: str, value: float = 1.0) -> None:
        log.info("metric increment name=%s value=%s", name, value)

    def observe(self, name: str, value: float) -> None:
        log.info("metric observe name=%s value=%s", name, value)
