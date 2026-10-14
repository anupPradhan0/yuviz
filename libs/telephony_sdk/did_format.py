"""Canonical DID form. A phone number ("+91 80-6535 4620", "(416) 555-0177") is stored and
looked up digits-only; anything else (a SIP URI, an alphanumeric extension) is kept as typed.

did:{did} is an exact-string Redis key, so the writer (services/config) and every reader
(services/telephony, campaigns) must canonicalise the same way. Carrier APIs that need
another form convert at their adapter (e.g. Vobiz's _e164)."""

from __future__ import annotations

import re

# Mirrored in database/schema.sql's DID backfill: same pattern, same trimmed whitespace.
_PHONE_SHAPED = re.compile(r"\+?[0-9 ().\-]*[0-9][0-9 ().\-]*")
_WHITESPACE = " \t\n\r\f\v"


def normalize_did(raw: str | None) -> str:
    did = (raw or "").strip(_WHITESPACE)
    if _PHONE_SHAPED.fullmatch(did):
        return re.sub(r"[^0-9]", "", did)
    return did


def is_routable_did(did: str) -> bool:
    """Canonical (digits-only) phone number, SIP URI, or a value with no digits at all
    (a named extension). Anything else holding digits, like "tel:+91..." or a number with an
    en dash, would never match the carrier's form."""
    return (
        did.isascii() and did.isdigit()
        or did.lower().startswith(("sip:", "sips:"))
        or not any(c.isdigit() for c in did)
    )
