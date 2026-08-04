"""Exceptions shared by every ITelephonyProvider implementation — mirrors
libs/knowledge_sdk/exceptions.py's shape (one narrow exception type per SDK,
not a generic Exception a caller has to guess at)."""

from __future__ import annotations


class TelephonyProviderError(Exception):
    """Raised for any provider-level failure (auth, malformed request,
    unreachable, invalid credentials) — never for a normal call outcome
    (busy/no-answer arrive as status callbacks, not exceptions)."""
