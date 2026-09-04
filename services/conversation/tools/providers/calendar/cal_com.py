"""
CalComCalendarProvider — ICalendarProvider backed by the real Cal.com v2
API. Every endpoint/shape/quirk below was confirmed against a live account
on 2026-07-22 (see project history) — not guessed from Cal.com's docs.

Real things that would otherwise bite in production, all handled here so
CalendarExecutor never has to know about them:
  - Cal.com sits behind Cloudflare, which returns error 1010 (a bot-
    signature block) against a bare Python HTTP client's default
    User-Agent — every call sets an explicit browser-like one.
  - /v2/bookings and /v2/slots require DIFFERENT cal-api-version header
    values — confirmed live, not a typo if they look inconsistent.
  - A booking-time conflict (the slot was taken between check_availability
    and book_appointment) comes back as a 400 BadRequestException with the
    message "User either already has booking at this time or is not
    available" — this is not distinguishable from "never available in the
    first place" by HTTP status alone, so this class raises a distinct
    SlotUnavailableError for it rather than a generic CalendarProviderError.

Phone-first identity (2026-07-27, confirmed live against the real API):
  - POST /v2/bookings accepts attendee.phoneNumber (E.164) with no email
    at all — Cal.com auto-generates its own internal placeholder email
    ("<digits>@sms.cal.com") for its own records; nothing to fake here.
  - GET /v2/bookings' attendeePhoneNumber query parameter is silently
    IGNORED server-side — confirmed by A/B testing: passing a phone number
    that matches nothing returned the exact same result set as passing no
    filter at all. Unlike attendeeEmail (confirmed working), phone-based
    lookup has to fetch upcoming bookings and match by phone client-side —
    see find_upcoming_bookings() below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import httpx

from .interface import (
    AttendeeInfo, AvailabilitySlot, BookingResult, BookingSummary, CalendarProviderError,
    InvalidAttendeePhoneError, SlotUnavailableError,
)

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cal.com"
# Cloudflare (error 1010) blocks default Python HTTP client signatures —
# see module docstring. Any real browser-like UA works; this one is
# arbitrary but stable.
_USER_AGENT = "Mozilla/5.0 (compatible; VoiceAIPlatform-CalendarExecutor/1.0)"
_BOOKINGS_API_VERSION = "2024-08-13"
_SLOTS_API_VERSION    = "2024-09-04"
_SLOT_CONFLICT_MESSAGE = "already has booking at this time or is not available"
# Confirmed live 2026-07-29: Cal.com's exact 400 message when
# attendee.phoneNumber isn't a valid phone number format — e.g. a 4-digit
# internal SIP extension used as the caller's ANI on a test call.
_INVALID_PHONE_MESSAGE = "attendeePhoneNumber}invalid_number"


class CalComCalendarProvider:
    def __init__(
        self,
        api_key:              str,
        event_type_id:        int,
        default_attendee_phone: str | None = None,
        default_attendee_email: str | None = None,
        default_timezone:     str = "UTC",
        base_url:             str = _DEFAULT_BASE_URL,
        timeout_s:            float = 10.0,
    ) -> None:
        self._event_type_id = event_type_id
        self._default_attendee_phone = default_attendee_phone
        self._default_attendee_email = default_attendee_email
        self._default_timezone = default_timezone
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT},
        )
        log.info("CalComCalendarProvider event_type_id=%s", event_type_id)

    @property
    def default_attendee_phone(self) -> str | None:
        return self._default_attendee_phone

    @property
    def default_attendee_email(self) -> str | None:
        return self._default_attendee_email

    @property
    def default_timezone(self) -> str:
        # Configurable per tool_provider_config (extra.timezone) instead of
        # a hardcoded "UTC" everywhere — a tenant outside UTC/US would
        # otherwise have every relative time silently misinterpreted.
        return self._default_timezone

    def requires_attendee_phone(self) -> bool:
        # Cal.com's own event-type bookingFields schema is the authoritative
        # source — this tenant's event type was reconfigured live at one
        # point to require attendeePhoneNumber (see module docstring). A
        # per-provider-instance flag set from that at construction is a
        # reasonable v1 simplification over querying the event type's live
        # schema on every call; revisit if an event type's requiredness
        # changes without redeploying this config. Confirmed live: this
        # event type's email requirement has flipped at least twice
        # (optional, then required again) with nothing here updated either
        # time — book_appointment() now always sends a placeholder email
        # unconditionally rather than trying to track that setting's drift.
        return True

    async def _slots_for_range(self, start_date: str, end_date: str, tz: str) -> dict[str, list[dict]]:
        resp = await self._client.get(
            "/v2/slots",
            params={
                "eventTypeId": self._event_type_id,
                "start": start_date,
                "end": end_date,
                "timeZone": tz,
            },
            headers={"cal-api-version": _SLOTS_API_VERSION},
        )
        if resp.status_code >= 400:
            raise CalendarProviderError(f"Cal.com /v2/slots returned {resp.status_code}: {resp.text}")
        return resp.json().get("data", {})

    async def check_availability(self, requested_datetime: str, timezone: str) -> bool:
        date_part = requested_datetime[:10]
        try:
            slots_by_date = await self._slots_for_range(date_part, date_part, timezone)
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc

        requested = _parse_local_iso(requested_datetime, timezone)
        for slot in slots_by_date.get(date_part, []):
            if _parse_iso(slot["start"]) == requested:
                return True
        return False

    async def find_available_slots(
        self, near_datetime: str, timezone: str, limit: int = 50,
    ) -> list[AvailabilitySlot]:
        # Scoped to near_datetime's own day only (confirmed live: this
        # used to search a 7-day window and cap the flattened result at
        # 3 total, so a caller asking "what's open tomorrow" could get
        # alternatives from several days out instead of a complete, honest
        # picture of tomorrow specifically). limit stays as a defensive
        # cap, not the primary scoping mechanism — raised well past any
        # realistic single day's slot count so it never truncates a real
        # day in practice; a day that's genuinely fully booked correctly
        # returns an empty list rather than silently borrowing from
        # another day.
        start_date = near_datetime[:10]
        try:
            slots_by_date = await self._slots_for_range(start_date, start_date, timezone)
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc

        flat = [slot["start"] for slot in slots_by_date.get(start_date, [])]
        return [AvailabilitySlot(start_iso=s) for s in flat[:limit]]

    async def book_appointment(
        self, slot_iso: str, attendee: AttendeeInfo, notes: str = "",
    ) -> BookingResult:
        # slot_iso is the same naive, caller-local wall-clock string
        # check_availability() received — must be localized to attendee.
        # timezone the same way before Cal.com ever sees it, or the booking
        # would land at the wrong absolute time (or simply never match a
        # real slot, per _parse_local_iso's docstring).
        start_utc_iso = _parse_local_iso(slot_iso, attendee.timezone).isoformat().replace("+00:00", "Z")
        attendee_name = attendee.name or "Caller"
        body = {
            "start": start_utc_iso,
            "eventTypeId": self._event_type_id,
            "attendee": {
                "name": attendee_name,
                "phoneNumber": attendee.phone or self._default_attendee_phone,
                "timeZone": attendee.timezone,
            },
        }
        # A real email, if the LLM somehow has one, always goes on the
        # attendee object itself too (distinct from bookingFieldsResponses
        # below — this is the actual invitee record Cal.com would use for
        # calendar invites, not a custom form question).
        if attendee.email:
            body["attendee"]["email"] = attendee.email
        # title and email are both required custom bookingFields on this
        # event type (confirmed live, in two separate incidents: title's
        # 400 "responses - {title}error_required_field" was found first,
        # and code added to always send one; months later, email's own
        # 400 "responses - {email}error_required_field" surfaced the same
        # way, because this tenant's event type has flipped email's
        # requiredness at least twice with nothing here ever updated to
        # match — see requires_attendee_phone()'s own comment). The
        # product deliberately never asks a phone caller for their email
        # (see the sales agent's own prompt), so there is usually no real
        # one to send — fall back to this tool_provider_config's own
        # default_attendee_email, and failing that, a synthesized
        # placeholder. Always send one regardless of whatever Cal.com's
        # event type currently requires: a placeholder costs nothing when
        # the field turns out to be optional, but its absence silently
        # hard-fails an otherwise-complete booking when required, exactly
        # as it did here.
        email = attendee.email or self._default_attendee_email or "caller@noreply.yuviz.ai"
        booking_fields_responses = {
            "title": f"Appointment for {attendee_name}",
            "email": email,
        }
        if notes:
            booking_fields_responses["notes"] = notes
        body["bookingFieldsResponses"] = booking_fields_responses

        # DEBUG, not INFO — this body carries the caller's phone/email;
        # httpx's own request logging never includes body content for any
        # method, only method/URL/status, so this is still the only way to
        # see what was actually sent, just not at a level that's on in prod.
        log.debug("Cal.com POST /v2/bookings body=%r", body)
        try:
            resp = await self._client.post(
                "/v2/bookings", json=body, headers={"cal-api-version": _BOOKINGS_API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc

        if resp.status_code >= 400:
            text = resp.text
            log.info("Cal.com /v2/bookings returned %s: %s", resp.status_code, text)
            if _SLOT_CONFLICT_MESSAGE in text:
                raise SlotUnavailableError(text)
            if _INVALID_PHONE_MESSAGE in text:
                raise InvalidAttendeePhoneError(text)
            raise CalendarProviderError(f"Cal.com /v2/bookings returned {resp.status_code}: {text}")

        data = resp.json().get("data", {})
        return BookingResult(
            booking_id=data.get("uid", ""),
            confirmed_slot=data.get("start", start_utc_iso),
            meeting_url=data.get("meetingUrl"),
        )

    async def cancel_appointment(self, booking_id: str, reason: str = "") -> None:
        try:
            resp = await self._client.post(
                f"/v2/bookings/{booking_id}/cancel",
                json={"cancellationReason": reason or "Cancelled"},
                headers={"cal-api-version": _BOOKINGS_API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise CalendarProviderError(f"Cal.com cancel returned {resp.status_code}: {resp.text}")

    async def find_upcoming_bookings(self, attendee_phone: str, limit: int = 5) -> list[BookingSummary]:
        # attendeePhoneNumber is NOT a working server-side filter — confirmed
        # live 2026-07-27 by A/B testing (see module docstring). Fetch this
        # event type's upcoming bookings (a generous take, since the real
        # filtering happens below) and match by phone client-side instead.
        # A wrong-tenant/wrong-event-type booking can never surface here
        # regardless, since eventTypeId already scopes the query.
        try:
            resp = await self._client.get(
                "/v2/bookings",
                params={
                    "eventTypeId": self._event_type_id,
                    "status": "upcoming",
                    "take": 100,
                },
                headers={"cal-api-version": _BOOKINGS_API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise CalendarProviderError(f"Cal.com /v2/bookings returned {resp.status_code}: {resp.text}")
        data = resp.json().get("data", [])
        target = _normalize_phone(attendee_phone)
        matches = [
            b for b in data
            if any(_normalize_phone(a.get("phoneNumber") or "") == target for a in b.get("attendees", []))
        ]
        return [BookingSummary(booking_id=b["uid"], start_iso=b["start"]) for b in matches[:limit]]

    async def reschedule_appointment(self, booking_id: str, new_slot_iso: str, timezone: str) -> BookingResult:
        # POST /v2/bookings/{uid}/reschedule confirmed live 2026-07-23: one
        # atomic call — Cal.com transitions the old booking to
        # status=cancelled and creates a new one (rescheduledFromUid links
        # them) server-side. A conflict on new_slot_iso returns the exact
        # same "already has booking..." message book_appointment's own
        # conflict race does, so it's mapped to SlotUnavailableError here
        # too, not a generic error.
        # new_slot_iso is the same naive, caller-local wall-clock string as
        # book_appointment's slot_iso — same localize-before-sending fix
        # (see _parse_local_iso's docstring).
        new_start_utc_iso = _parse_local_iso(new_slot_iso, timezone).isoformat().replace("+00:00", "Z")
        try:
            resp = await self._client.post(
                f"/v2/bookings/{booking_id}/reschedule",
                json={"start": new_start_utc_iso},
                headers={"cal-api-version": _BOOKINGS_API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise CalendarProviderError(f"Cal.com unreachable: {exc}") from exc

        if resp.status_code >= 400:
            text = resp.text
            if _SLOT_CONFLICT_MESSAGE in text:
                raise SlotUnavailableError(text)
            raise CalendarProviderError(f"Cal.com reschedule returned {resp.status_code}: {text}")

        data = resp.json().get("data", {})
        return BookingResult(
            booking_id=data.get("uid", ""),
            confirmed_slot=data.get("start", new_start_utc_iso),
            meeting_url=data.get("meetingUrl"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_iso(value: str) -> datetime:
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc).replace(microsecond=0)


def _parse_local_iso(value: str, tz_name: str) -> datetime:
    """Same as _parse_iso, except a naive (no-offset) value — what the LLM
    always sends for requested_datetime/slot_iso, per the tool schema's own
    example ("2026-07-23T15:00:00") — is interpreted as wall-clock time IN
    tz_name, not UTC. Cal.com's /v2/slots returns real slot starts as
    absolute UTC instants, so comparing a naive-as-UTC parse against them
    was off by exactly the caller's UTC offset (e.g. 4-5h for US zones) and
    check_availability() could never match, silently forcing every booking
    attempt down the 'not available, here are alternatives' path — a
    real bug found 2026-08-01 via a live Cal.com booking that never posted.
    A value that already carries an explicit offset/Z is trusted as-is."""
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(dt_timezone.utc).replace(microsecond=0)


def _normalize_phone(phone: str) -> str:
    """Compares by the last 10 digits (US national significant number)
    rather than exact string equality — a caller's ANI arrives as strict
    E.164 ("+14155551234") but a caller stating their number aloud for
    cancel/reschedule, or a booking made through some other channel, may
    come through without the country code or with formatting punctuation.
    Deliberately US-centric for now, same scope as the rest of this
    tenant's config; revisit if a non-US tenant needs this provider."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits
