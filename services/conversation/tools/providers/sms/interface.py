"""
ISmsProvider — boundary CalendarExecutor uses to send a booking
confirmation text, hiding the concrete vendor (Twilio today). Optional
end-to-end: None means no credentials configured, just skip sending.
"""

from __future__ import annotations

from typing import Protocol


class SmsProviderError(Exception):
    """Raised for any provider-side failure — always caught by the caller."""


class ISmsProvider(Protocol):
    async def send_sms(self, to_number: str, body: str) -> None:
        """Raises SmsProviderError on failure. No return value."""
        ...
