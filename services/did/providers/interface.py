"""
IDidProvider — the boundary the DID Service coordinates and every concrete
carrier (Twilio, Plivo, Vonage — matching carriers.provider's CHECK
constraint) hides behind. Plays the same role ICalendarProvider plays for
CalendarExecutor, ISTT/ILLM/ITTS play for the pipeline: the REST routers
and DidProviderManager never import a concrete implementation.

See project memory did-management-platform-architecture principle #11 —
this mirrors ai_provider_manager.py's registry-of-factories pattern, not a
growing if/elif chain.

No concrete provider is implemented yet (2026-07-23): the first one
(Plivo) is deliberately not written until real trial-account credentials
exist to confirm live behavior against, the same discipline already
applied to Cal.com and Gemini in this project — never guess a vendor API
shape from documentation alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AvailableNumber:
    phone_number:  str  # E.164, e.g. "+14155551234"
    region:        str | None = None
    monthly_price: str | None = None  # carrier's own currency string, display-only, never parsed
    capabilities:  tuple[str, ...] = ()  # e.g. ("voice", "sms")


@dataclass(frozen=True)
class PurchasedNumber:
    phone_number:       str
    carrier_number_sid: str  # carrier's own id for this number — needed to release() it later


class DidProviderError(Exception):
    """Raised for any provider-side failure (unreachable, auth, malformed
    request, or the carrier rejecting a purchase/release outright — e.g.
    the number was already taken by someone else between search and
    purchase). Never raised for "no numbers matched the search" — that's a
    real empty-list return, not an error (same posture as
    ICalendarProvider.find_available_slots())."""


class IDidProvider(Protocol):
    async def search_available_numbers(
        self, country: str, area_code: str | None = None, limit: int = 10,
    ) -> list[AvailableNumber]:
        """Live search against the carrier — never cached, never
        speculative; a result you can actually purchase right now.
        Empty list is a valid, non-error result."""
        ...

    async def purchase_number(self, phone_number: str) -> PurchasedNumber:
        """Raises DidProviderError if the number is no longer available
        (race between search and purchase) or on any other carrier-side
        failure — there is no separate 'already taken' return value,
        unlike book_appointment's SlotUnavailableError split, because a
        failed purchase has no useful alternative to fall through to the
        way an unavailable calendar slot does; the caller just searches
        again."""
        ...

    async def release_number(self, carrier_number_sid: str) -> None:
        """Relinquishes the number back to the carrier — called once an
        admin releases a purchased_numbers row (see purchased_numbers.py).
        Idempotent from the caller's perspective: releasing an
        already-released number should not be treated as success/failure
        ambiguity the caller has to guess at — a concrete provider should
        raise DidProviderError only for genuine failures, not for
        already-gone."""
        ...
