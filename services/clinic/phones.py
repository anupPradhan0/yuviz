"""Phone-number normalisation. Every number the clinic stores or compares is E.164."""

from __future__ import annotations

import re

_DEFAULT_COUNTRY_CODE = "91"


def normalize_phone(raw: str | None, *, country_code: str = _DEFAULT_COUNTRY_CODE) -> str | None:
    """'+91 98765-43210', '09876543210', '919876543210' and '9876543210' all become
    '+919876543210'. Returns None for anything that is not a plausible number, so a
    bad value never matches someone else's booking."""
    if not raw:
        return None
    text = str(raw).strip()
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    # Toolexec's caller_id is '+' and whatever digits the carrier sent, so a national
    # '08528661721' arrives as '+08528661721'. No country code starts with 0.
    if has_plus and digits.startswith("0") and not digits.startswith("00"):
        has_plus = False
    if has_plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if digits.startswith("00"):
        digits = digits[2:]
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if country_code == "91":
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"+91{digits}"
        if len(digits) == 12 and digits.startswith("91"):
            return f"+{digits}"
        return None
    return f"+{country_code}{digits.lstrip('0')}" if 6 <= len(digits) <= 13 else None


def spoken_last_digits(phone: str, count: int = 4) -> str:
    """'+919876543210' -> '3 2 1 0': enough to recognise the number without reading it all out."""
    return " ".join(phone[-count:])
