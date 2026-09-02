"""
ISmsProvider — the boundary CalendarExecutor uses to send a booking
confirmation text, hiding whichever concrete SMS vendor (Twilio today)
sits behind it. Same role ICalendarProvider plays for Cal.com/Calendly/etc
— executors never import a concrete implementation.

Deliberately optional end-to-end: CalendarExecutor accepts `sms_provider:
ISmsProvider | None = None` and simply skips sending when it's None (no
credentials configured) rather than failing. A send failure is likewise
never allowed to affect the booking's own ToolResult — see
calendar_executor.py's own try/except around the send call for why: the
caller already has a real appointment at this point, and losing that
confirmation because a text message failed to send would be a strictly
worse bug than the missing text itself.
"""

from __future__ import annotations

from typing import Protocol


class SmsProviderError(Exception):
    """Raised for any provider-side failure (unreachable, auth, rejected
    number). Always caught by the caller (see module docstring) — this
    exists so a concrete provider has something specific to raise, not so
    CalendarExecutor can react differently to different failure modes."""


class ISmsProvider(Protocol):
    async def send_sms(self, to_number: str, body: str) -> None:
        """Raises SmsProviderError on any failure. No return value — there
        is no follow-up action taken on success today (no delivery-receipt
        tracking, no retry queue)."""
        ...
