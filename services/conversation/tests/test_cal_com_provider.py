"""
CalComCalendarProvider tests use httpx.MockTransport — no real network
call, no cost. Every mocked response shape here matches what was actually
captured live against a real Cal.com account on 2026-07-22 (see project
history: check_availability, find_available_slots, book_appointment, and
cancel_appointment were all smoke-tested against the live API before these
mocked regression tests were written).
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.conversation.tools.providers.calendar.cal_com import CalComCalendarProvider
from services.conversation.tools.providers.calendar.interface import (
    AttendeeInfo, CalendarProviderError, InvalidAttendeePhoneError, SlotUnavailableError,
)

_SLOTS_RESPONSE = {
    "data": {
        "2026-07-23": [
            {"start": "2026-07-23T10:00:00.000Z"},
            {"start": "2026-07-23T10:15:00.000Z"},
            {"start": "2026-07-23T10:30:00.000Z"},
        ],
    },
    "status": "success",
}


def _make_provider(handler, **kwargs) -> CalComCalendarProvider:
    provider = CalComCalendarProvider(api_key="test-key", event_type_id=123, **kwargs)
    provider._client = httpx.AsyncClient(base_url="https://api.cal.com", transport=httpx.MockTransport(handler))
    return provider


async def test_check_availability_true_for_a_returned_slot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cal-api-version"] == "2024-09-04"
        return httpx.Response(200, json=_SLOTS_RESPONSE)

    provider = _make_provider(handler)
    assert await provider.check_availability("2026-07-23T10:00:00.000Z", "UTC") is True


async def test_check_availability_false_for_a_slot_not_in_the_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SLOTS_RESPONSE)

    provider = _make_provider(handler)
    assert await provider.check_availability("2026-07-23T17:00:00.000Z", "UTC") is False


async def test_check_availability_localizes_a_naive_datetime_to_the_given_timezone():
    # Real bug found live 2026-08-01: the LLM always sends a naive
    # wall-clock string (no offset) — "6am America/New_York" is
    # "2026-07-23T06:00:00", which is 10:00 UTC. Cal.com's slots are real
    # UTC instants. Before the fix, the naive string was mis-parsed as
    # already-UTC and never matched a real slot, so book_appointment() was
    # never reached for any tenant outside UTC.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SLOTS_RESPONSE)

    provider = _make_provider(handler)
    assert await provider.check_availability("2026-07-23T06:00:00", "America/New_York") is True


async def test_find_available_slots_scoped_to_requested_day_only():
    """Confirmed live: this used to query a 7-day window and flatten the
    result across all of them, so a caller asking "what's open tomorrow"
    could get alternatives from several days out — not what they asked
    about. The Cal.com request itself must now span exactly one day."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["start"] == "2026-07-23"
        assert request.url.params["end"] == "2026-07-23"
        return httpx.Response(200, json=_SLOTS_RESPONSE)

    provider = _make_provider(handler)
    slots = await provider.find_available_slots("2026-07-23T09:00:00.000Z", "UTC")

    assert [s.start_iso for s in slots] == [
        "2026-07-23T10:00:00.000Z", "2026-07-23T10:15:00.000Z", "2026-07-23T10:30:00.000Z",
    ]


async def test_find_available_slots_returns_up_to_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SLOTS_RESPONSE)

    provider = _make_provider(handler)
    slots = await provider.find_available_slots("2026-07-23T09:00:00.000Z", "UTC", limit=2)

    assert [s.start_iso for s in slots] == ["2026-07-23T10:00:00.000Z", "2026-07-23T10:15:00.000Z"]


async def test_book_appointment_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cal-api-version"] == "2024-08-13"
        body = json.loads(request.content)
        assert body["attendee"]["phoneNumber"] == "+14155551234"
        assert "email" not in body["attendee"]
        # title is a required custom bookingField on real event types —
        # found live 2026-08-02 as a genuine production bug: every booking
        # attempt was returning 400 "responses - {title}error_required_field"
        # because this was never sent at all.
        assert body["bookingFieldsResponses"]["title"] == "Appointment for Jane"
        return httpx.Response(200, json={
            "status": "success",
            "data": {"uid": "abc123", "start": "2026-07-24T10:00:00.000Z", "meetingUrl": "https://app.cal.com/video/abc123"},
        })

    provider = _make_provider(handler)
    result = await provider.book_appointment(
        "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="+14155551234"),
    )

    assert result.booking_id == "abc123"
    assert result.meeting_url == "https://app.cal.com/video/abc123"


async def test_book_appointment_sends_placeholder_email_when_none_configured():
    # Real production bug, confirmed live: Cal.com rejected every booking
    # attempt with 400 "responses - {email}error_required_field" — this
    # event type requires email as a custom bookingField (its requiredness
    # has flipped at least twice with nothing ever updated to match, see
    # cal_com.py's own comment), and nothing here ever sent one at all. The
    # product deliberately never asks a phone caller for their email, so a
    # synthesized placeholder must always be sent regardless.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["bookingFieldsResponses"]["email"] == "caller@noreply.yuviz.ai"
        return httpx.Response(200, json={"status": "success", "data": {"uid": "abc123"}})

    provider = _make_provider(handler)
    await provider.book_appointment(
        "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="+14155551234"),
    )


async def test_book_appointment_uses_configured_default_attendee_email():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["bookingFieldsResponses"]["email"] == "leads@acmerealty.example.com"
        return httpx.Response(200, json={"status": "success", "data": {"uid": "abc123"}})

    provider = _make_provider(handler, default_attendee_email="leads@acmerealty.example.com")
    await provider.book_appointment(
        "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="+14155551234"),
    )


async def test_book_appointment_uses_a_real_attendee_email_when_given():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["bookingFieldsResponses"]["email"] == "jane@example.com"
        assert body["attendee"]["email"] == "jane@example.com"
        return httpx.Response(200, json={"status": "success", "data": {"uid": "abc123"}})

    provider = _make_provider(handler, default_attendee_email="leads@acmerealty.example.com")
    await provider.book_appointment(
        "2026-07-24T10:00:00.000Z",
        AttendeeInfo(name="Jane", phone="+14155551234", email="jane@example.com"),
    )


async def test_book_appointment_localizes_a_naive_datetime_to_the_attendee_timezone():
    # Same bug as check_availability's naive-datetime case, but on the
    # actual POST body — sending the naive local string straight to
    # Cal.com would have booked the wrong absolute time.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["start"] == "2026-07-24T14:00:00Z"
        return httpx.Response(200, json={
            "status": "success",
            "data": {"uid": "abc123", "start": "2026-07-24T14:00:00.000Z"},
        })

    provider = _make_provider(handler)
    result = await provider.book_appointment(
        "2026-07-24T10:00:00",
        AttendeeInfo(name="Jane", phone="+14155551234", timezone="America/New_York"),
    )
    assert result.booking_id == "abc123"


async def test_book_appointment_includes_notes_alongside_title_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["bookingFieldsResponses"]["title"] == "Appointment for Caller"
        assert body["bookingFieldsResponses"]["notes"] == "Prefers a window seat"
        return httpx.Response(200, json={"status": "success", "data": {"uid": "abc123"}})

    provider = _make_provider(handler)
    await provider.book_appointment(
        "2026-07-24T10:00:00.000Z", AttendeeInfo(name="", phone="+14155551234"),
        notes="Prefers a window seat",
    )


async def test_book_appointment_conflict_raises_slot_unavailable_not_generic_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "status": "error",
            "error": {
                "code": "BadRequestException",
                "message": "User either already has booking at this time or is not available",
            },
        })

    provider = _make_provider(handler)
    with pytest.raises(SlotUnavailableError):
        await provider.book_appointment(
            "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="+14155551234"),
        )


async def test_book_appointment_invalid_phone_raises_invalid_attendee_phone_error():
    # The exact response body captured live 2026-07-29, from a real test
    # call whose ANI (a 4-digit internal SIP extension) Cal.com correctly
    # rejected as not a real phone number.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "status": "error",
            "timestamp": "2026-07-29T17:26:28.991Z",
            "path": "/v2/bookings",
            "error": {"code": "BAD_REQUEST", "message": "responses - {attendeePhoneNumber}invalid_number, "},
        })

    provider = _make_provider(handler)
    with pytest.raises(InvalidAttendeePhoneError):
        await provider.book_appointment(
            "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="1001"),
        )


async def test_book_appointment_other_4xx_raises_generic_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error", "error": {"message": "invalid API key"}})

    provider = _make_provider(handler)
    with pytest.raises(CalendarProviderError):
        await provider.book_appointment(
            "2026-07-24T10:00:00.000Z", AttendeeInfo(name="Jane", phone="+14155551234"),
        )


async def test_cancel_appointment_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/cancel")
        return httpx.Response(200, json={"status": "success", "data": {"status": "cancelled"}})

    provider = _make_provider(handler)
    await provider.cancel_appointment("abc123", reason="test")  # must not raise


async def test_network_failure_raises_calendar_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _make_provider(handler)
    with pytest.raises(CalendarProviderError):
        await provider.check_availability("2026-07-23T10:00:00.000Z", "UTC")


async def test_find_upcoming_bookings_matches_by_phone_client_side():
    # attendeePhoneNumber is NOT a working server-side filter — confirmed
    # live 2026-07-27 by A/B testing (passing a phone matching nothing
    # returned the exact same result set as passing none at all). This
    # provider therefore fetches everything upcoming for the event type and
    # matches by phone itself — the mocked response below deliberately
    # includes a booking under a DIFFERENT phone number to prove the
    # client-side filter actually excludes it, not just passes everything
    # through.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cal-api-version"] == "2024-08-13"
        assert "attendeePhoneNumber" not in request.url.params
        assert request.url.params["status"] == "upcoming"
        return httpx.Response(200, json={
            "status": "success",
            "data": [
                {"id": 1, "uid": "abc123", "start": "2026-07-24T10:00:00.000Z",
                 "attendees": [{"phoneNumber": "+14155551234"}]},
                {"id": 2, "uid": "def456", "start": "2026-07-25T14:00:00.000Z",
                 "attendees": [{"phoneNumber": "+14155551234"}]},
                {"id": 3, "uid": "someoneElse", "start": "2026-07-26T10:00:00.000Z",
                 "attendees": [{"phoneNumber": "+19998887777"}]},
            ],
        })

    provider = _make_provider(handler)
    bookings = await provider.find_upcoming_bookings("+14155551234")

    assert [b.booking_id for b in bookings] == ["abc123", "def456"]
    assert bookings[0].start_iso == "2026-07-24T10:00:00.000Z"


async def test_find_upcoming_bookings_matches_regardless_of_formatting():
    # A caller stating their number aloud, or a booking's ANI captured in a
    # slightly different format, shouldn't fail to match on formatting
    # alone — compares by the last 10 digits, not exact string equality.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": [
                {"id": 1, "uid": "abc123", "start": "2026-07-24T10:00:00.000Z",
                 "attendees": [{"phoneNumber": "(415) 555-1234"}]},
            ],
        })

    provider = _make_provider(handler)
    bookings = await provider.find_upcoming_bookings("+14155551234")

    assert [b.booking_id for b in bookings] == ["abc123"]


async def test_find_upcoming_bookings_empty_result_is_empty_list_not_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": []})

    provider = _make_provider(handler)
    assert await provider.find_upcoming_bookings("+19995550000") == []


async def test_find_upcoming_bookings_4xx_raises_calendar_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error", "error": {"message": "invalid API key"}})

    provider = _make_provider(handler)
    with pytest.raises(CalendarProviderError):
        await provider.find_upcoming_bookings("+14155551234")


async def test_reschedule_appointment_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reschedule")
        assert request.headers["cal-api-version"] == "2024-08-13"
        return httpx.Response(200, json={
            "status": "success",
            "data": {
                "uid": "newUid123", "start": "2026-07-28T03:30:00.000Z",
                "meetingUrl": "https://app.cal.com/video/newUid123",
            },
        })

    provider = _make_provider(handler)
    result = await provider.reschedule_appointment("oldUid456", "2026-07-28T03:30:00.000Z", "UTC")

    assert result.booking_id == "newUid123"
    assert result.confirmed_slot == "2026-07-28T03:30:00.000Z"
    assert result.meeting_url == "https://app.cal.com/video/newUid123"


async def test_reschedule_appointment_conflict_raises_slot_unavailable_not_generic_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "status": "error",
            "error": {
                "code": "BadRequestException",
                "message": "User either already has booking at this time or is not available",
            },
        })

    provider = _make_provider(handler)
    with pytest.raises(SlotUnavailableError):
        await provider.reschedule_appointment("oldUid456", "2026-07-28T03:30:00.000Z", "UTC")


async def test_reschedule_appointment_other_4xx_raises_generic_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "status": "error",
            "error": {"message": "Can't reschedule booking because it has been cancelled."},
        })

    provider = _make_provider(handler)
    with pytest.raises(CalendarProviderError):
        await provider.reschedule_appointment("oldUid456", "2026-07-28T03:30:00.000Z", "UTC")
