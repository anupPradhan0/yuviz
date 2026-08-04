"""
GuardrailDetector — deterministic, inline caller-frustration/abuse signal.

This is the detector record_guardrail_violation() (pipeline.py) was built
to receive: it runs on the caller's transcript string already in hand —
pure regex matching, microseconds, no LLM call, no network, nothing on the
media path — matching the platform's hot-path rules (no added latency, no
sync I/O, deterministic).

Scope deliberately v1: a curated English phrase lexicon in two categories.
It errs toward precision over recall — a missed frustration cue costs one
un-counted violation; a false positive erodes trust in the escalation
counter. Word-boundary, case-insensitive matching only; no stemming, no
sentiment scoring, no per-tenant customization yet (that would be a
tenant/agent-level column feeding custom patterns — backlog, same shape as
every other config override in this codebase).

Detection always runs (violations are counted and logged for
observability even when escalation_threshold is NULL/disabled — see
record_guardrail_violation's docstring); an actual transfer only fires
when the agent's Escalation config says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit frustration with the AI/conversation — the caller is telling us
# the interaction is failing.
_FRUSTRATION_PHRASES = [
    r"this is useless",
    r"this is ridiculous",
    r"this is pointless",
    r"(?:you'?re|you are) not helping",
    r"not helping at all",
    r"(?:you'?re|you are) not listening",
    r"you don'?t understand",
    r"i already told you",
    r"you keep saying the same",
    r"i give up",
    r"waste of (?:my )?time",
    r"(?:i'?m|i am) (?:getting )?frustrated",
    r"this is frustrating",
    r"sick of this",
    r"fed up",
    r"stop repeating",
    # Added 2026-07-18 from live-call misses (a caller expressed clear
    # dissatisfaction that v1 didn't catch — see project memory):
    r"not satisfied",
    r"not helpful",
    r"(?:isn'?t|is not|not) working",
    r"doesn'?t work",
    r"doesn'?t help",
    # "X is (completely|absolutely|totally|just) ridiculous/useless" — the
    # live calls said "your service is completely ridiculous", which the
    # this-is-only patterns above missed.
    r"(?:is|was) (?:completely|absolutely|totally|just) (?:ridiculous|useless)",
]

# Abuse / profanity directed at the agent — a strong signal the caller is
# past the point where the AI should keep trying alone.
_ABUSE_PHRASES = [
    r"fuck(?:ing|ed)?",
    r"shit",
    r"bullshit",
    r"bastard",
    r"asshole",
    r"idiot",
    r"stupid (?:bot|machine|robot|thing|ai)",
    r"shut up",
    r"piece of (?:crap|junk|garbage)",
]

_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("frustration", re.compile(r"\b(?:" + "|".join(_FRUSTRATION_PHRASES) + r")\b", re.IGNORECASE)),
    ("abuse",       re.compile(r"\b(?:" + "|".join(_ABUSE_PHRASES) + r")\b", re.IGNORECASE)),
]


@dataclass(frozen=True)
class GuardrailViolation:
    category: str  # "frustration" | "abuse"
    matched:  str  # the exact text span that fired — for logs, never spoken


class GuardrailDetector:
    """check() returns the first violation found in the caller's utterance,
    or None. One utterance = at most one violation (multiple hits in the
    same sentence are one signal of one unhappy turn, not several)."""

    @staticmethod
    def check(text: str) -> GuardrailViolation | None:
        if not text:
            return None
        for category, pattern in _CATEGORIES:
            m = pattern.search(text)
            if m:
                return GuardrailViolation(category=category, matched=m.group(0))
        return None


class GuardrailCounter:
    """Per-session *consecutive* violation count — deliberately separate
    from TransferDecisionEngine (see transfer_engine.py's module
    docstring): the engine answers "given this count, should we
    transfer?", never "what is the current count?" or "was this a
    violation?". This class answers the latter two, and nothing else —
    it has no notion of thresholds or transfers.

    increment() bumps and returns the new count; reset() (call on any
    turn that produced no violation) zeroes it, so a caller who is
    frustrated once, then satisfied, then frustrated again starts counting
    from 1 again rather than accumulating across the whole call."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def increment(self, session_id: str) -> int:
        count = self._counts.get(session_id, 0) + 1
        self._counts[session_id] = count
        return count

    def reset(self, session_id: str) -> None:
        self._counts.pop(session_id, None)

    def current(self, session_id: str) -> int:
        return self._counts.get(session_id, 0)

    # Alias for call sites cleaning up at session end, where "reset" would
    # misleadingly imply the session continues — same operation.
    forget = reset
