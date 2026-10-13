"""Defensive environment parsing for the optional Phase 7 features. A bad value (empty, text, inf, nan, negative, out of
range) must never take retrieval or ingestion down: it falls back to the documented default and logs ONE warning
per variable. The value itself is not logged (an env var can hold a secret ref by mistake)."""

from __future__ import annotations

import logging
import math
import os

log = logging.getLogger(__name__)
_warned: set[str] = set()


def _warn(name: str) -> None:
    if name not in _warned:
        _warned.add(name)
        log.warning("%s is not a usable value (need a finite number in range); using the default", name)


def env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 1e9) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if raw is not None:
            _warn(name)
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn(name)
        return default
    if not math.isfinite(value) or value < lo or value > hi:
        _warn(name)
        return default
    return value


def env_int(name: str, default: int, *, lo: int = 0, hi: int = 10**9) -> int:
    return int(env_float(name, float(default), lo=float(lo), hi=float(hi)))


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "0").strip().lower() in ("1", "true", "yes", "on")
