"""
ICalendarProvider — the boundary CalendarExecutor/CancelAppointmentExecutor
coordinate and every concrete calendar vendor (Cal.com, Calendly, Google
Calendar, ...) hides behind. Plays the exact same role ISTT/ILLM/ITTS
already play for the STT/LLM/TTS pipeline — the executors and everything
above them never import a concrete implementation.

cancel_appointment (2026-07-23): "caller verification" for v1 means
matching by attendee email — the same trust model book_appointment already
uses (whatever email the caller states is trusted, not independently
verified). find_upcoming_bookings() resolves "my appointment" into a
concrete booking_id for cancel_appointment()/reschedule_appointment() to
act on; see CancelAppointmentExecutor/RescheduleAppointmentExecutor for the
disambiguation flow when a caller has more than one upcoming booking.

reschedule_appointment (2026-07-23): originally deferred as "find + cancel
+ book, each with its own partial-failure modes" — turned out to be moot.
Cal.com's real v2 API has a native atomic reschedule endpoint (confirmed
live) that does both sides in one call, server-side, with no window for
"new booked but old not cancelled" or vice versa. So this is one call, not
three composed ones — the original reason for deferring no longer applies.

Phone-first identity (2026-07-27): email dictation over voice was the
single largest source of booking failures all session (letter-by-letter
spelling mis-heard by STT, or mangled/hallucinated by weaker LLMs — the
tool executors used to carry a dedicated normalizer for this, since
deleted along with the email-based flow it existed for). Confirmed live
against Cal.com's real API that its v2 event-type bookingFields schema
supports a first-class
attendeePhoneNumber field, and reconfigured this tenant's event type to
require phone instead of email (Cal.com auto-generates its own internal
placeholder email like "<phone>@sms.cal.com" — nothing to fake on our
side). book_appointment now uses the caller's real ANI (SIP caller ID,
threaded through as ToolExecutionContext.caller_number) automatically,
with no need to ask — a live call always has one. cancel_appointment/
reschedule_appointment deliberately do NOT trust the ANI, even though it's
available: a caller phoning in to cancel may not be calling from the same
number they booked with, so those two always ask the caller to state a
phone number instead (see their own executor docstrings). One real API
limitation discovered live: Cal.com's GET /v2/bookings does NOT actually
filter by attendeePhoneNumber server-side (confirmed by A/B testing —
passing a phone number that matches nothing returned the exact same
result set as passing none at all), unlike attendeeEmail which is
confirmed to filter correctly. find_upcoming_bookings() therefore fetches
upcoming bookings and matches by phone client-side in cal_com.py, not via
a query parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AvailabilitySlot:
    start_iso: str  # ISO 8601, e.g. "2026-07-23T15:00:00.000Z"


@dataclass(frozen=True)
class BookingResult:
    booking_id:   str
    confirmed_slot: str
    meeting_url:  str | None = None


@dataclass(frozen=True)
class AttendeeInfo:
    name:  str
    phone: str
    timezone: str = "UTC"
    email: str | None = None  # only ever set as a defense-in-depth fallback — see book_appointment()


@dataclass(frozen=True)
class BookingSummary:
    booking_id: str  # same id space cancel_appointment()'s booking_id expects
    start_iso:  str


class CalendarProviderError(Exception):
    """Raised for any provider-side failure (unreachable, auth, malformed
    request) — CalendarExecutor catches this and maps it to
    ToolStatus.FAILED. Never raised for "slot unavailable" — that's a real
    return value (see check_availability/book_appointment docstrings),
    not an exception."""


class SlotUnavailableError(Exception):
    """Raised by book_appointment specifically for the book-time-conflict
    race (see design §12) — check_availability said available, but the
    slot was taken before the booking call landed. CalendarExecutor treats
    this identically to check_availability() returning False: fall through
    to find_available_slots(), not FAILED."""


class InvalidAttendeePhoneError(Exception):
    """Raised by book_appointment specifically when the provider rejects
    the attendee phone number as malformed (Cal.com: 400 "attendeePhoneNumber
    invalid_number") — confirmed live 2026-07-29 when a test call's ANI was
    a 4-digit internal SIP extension, not a real phone number. Distinct
    from CalendarProviderError (an outage/auth/malformed-request failure
    the caller can't do anything about) because THIS one the caller CAN
    fix: CalendarExecutor catches it and asks for a different phone number
    instead of collapsing it into a generic "calendar unreachable" — the
    same asked-for-a-number recovery path already used when no ANI exists
    at all, just triggered by "the ANI we had was rejected" instead of "we
    had none"."""


class ICalendarProvider(Protocol):
    async def check_availability(self, requested_datetime: str, timezone: str) -> bool:
        """True if requested_datetime is bookable right now. Never raises
        for 'not available' — only for a genuine provider-side failure
        (see CalendarProviderError)."""
        ...

    async def find_available_slots(
        self, near_datetime: str, timezone: str, limit: int = 50,
    ) -> list[AvailabilitySlot]:
        """Candidate alternatives on near_datetime's own day specifically
        (not a multi-day window — see cal_com.py's implementation comment
        for why). An empty list is a valid, non-error result (see design
        §12: 'not available, and alternatives lookup also came up empty'
        still degrades to SUCCESS/booked=false with an empty list, not
        FAILED) — correctly meaning that day is fully booked, not silently
        borrowed from a different day."""
        ...

    async def book_appointment(
        self, slot_iso: str, attendee: AttendeeInfo, notes: str = "",
    ) -> BookingResult:
        """Raises SlotUnavailableError on the book-time-conflict race,
        CalendarProviderError on any other provider-side failure."""
        ...

    async def cancel_appointment(self, booking_id: str, reason: str = "") -> None:
        """Called by CancelAppointmentExecutor once find_upcoming_bookings()
        has resolved a single concrete booking_id — see module docstring."""
        ...

    async def find_upcoming_bookings(self, attendee_phone: str, limit: int = 5) -> list[BookingSummary]:
        """Upcoming (not started, not cancelled) bookings for this exact
        attendee phone number — how CancelAppointmentExecutor turns 'cancel
        my appointment' into a concrete booking_id. An empty list is a
        valid, non-error result (no upcoming booking for that number), same
        posture as find_available_slots(). Matched client-side by the
        concrete implementation, not via a server-side filter — see module
        docstring's "Phone-first identity" note on why."""
        ...

    async def reschedule_appointment(self, booking_id: str, new_slot_iso: str, timezone: str) -> BookingResult:
        """Atomically moves an existing booking to new_slot_iso — one
        provider-side call, not cancel()+book_appointment() composed here.
        Raises SlotUnavailableError on the same book-time-conflict race
        book_appointment() raises (confirmed live: identical error message
        from Cal.com), CalendarProviderError on any other failure."""
        ...

    def requires_attendee_phone(self) -> bool:
        """Whether this tenant's configured event type requires an
        attendee phone number — the business-rule-level validation layer
        CalendarExecutor.validate() checks. Only matters when the caller_
        number (ANI) is unavailable (e.g. a webcall/browser test session),
        since a real telephony call always has one and never needs to ask."""
        ...

    @property
    def default_attendee_phone(self) -> str | None:
        """The tenant-configured fallback (tool_provider_configs.extra.
        default_attendee_phone) — CalendarExecutor checks this explicitly
        before deciding whether to ask the caller, rather than relying on
        book_appointment()'s own silent fallback (which stays, as
        defense in depth, not as the primary decision point)."""
        ...

    @property
    def default_timezone(self) -> str:
        """The tenant-configured default timezone (tool_provider_configs.
        extra.timezone, admin-configurable in the Tools tab — see
        ToolsPanel.tsx) used whenever the caller/LLM doesn't specify one.
        Was hardcoded "UTC" everywhere until 2026-07-27; a tenant outside
        UTC/US would otherwise have every relative time silently
        misinterpreted."""
        ...
