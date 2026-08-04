"""
CancelAppointmentExecutor tests — pure unit tests against a fake
ICalendarProvider, no network at all.
"""

from __future__ import annotations

from services.conversation.tools.executors.cancel_appointment_executor import CancelAppointmentExecutor
from services.conversation.tools.providers.calendar.interface import BookingSummary, CalendarProviderError
from services.conversation.tools.types import ToolExecutionContext, ToolExecutionRequest, ToolStatus


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1",
        turn_id="turn1", tool_iteration=0, deadline=0.0, request_id="r1",
    )


def _request(**args) -> ToolExecutionRequest:
    return ToolExecutionRequest(tool_call_id="call1", tool_name="cancel_appointment", arguments=args, context=_ctx())


class _FakeProvider:
    def __init__(
        self, *, bookings: list[BookingSummary] | None = None,
        find_raises: Exception | None = None, cancel_raises: Exception | None = None,
    ) -> None:
        self._bookings = bookings if bookings is not None else []
        self._find_raises = find_raises
        self._cancel_raises = cancel_raises
        self.cancelled_booking_id: str | None = None

    async def find_upcoming_bookings(self, attendee_phone, limit=5):
        if self._find_raises:
            raise self._find_raises
        return self._bookings

    async def cancel_appointment(self, booking_id, reason=""):
        if self._cancel_raises:
            raise self._cancel_raises
        self.cancelled_booking_id = booking_id


async def test_missing_attendee_phone_is_invalid_argument():
    executor = CancelAppointmentExecutor(_FakeProvider())
    result = await executor.execute(_request())

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["attendee_phone"]


async def test_no_upcoming_booking_is_success_not_failed():
    executor = CancelAppointmentExecutor(_FakeProvider(bookings=[]))
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload == {"cancelled": False, "reason": "no_upcoming_booking"}


async def test_single_upcoming_booking_cancels_without_needing_a_hint():
    booking = BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking])
    executor = CancelAppointmentExecutor(provider)
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["cancelled"] is True
    assert result.payload["booking_id"] == "b1"
    assert provider.cancelled_booking_id == "b1"


async def test_multiple_bookings_with_no_hint_asks_instead_of_guessing():
    bookings = [
        BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z"),
        BookingSummary(booking_id="b2", start_iso="2026-07-25T14:00:00.000Z"),
    ]
    executor = CancelAppointmentExecutor(_FakeProvider(bookings=bookings))
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["cancelled"] is False
    assert result.payload["reason"] == "multiple_bookings_found"
    assert len(result.payload["upcoming_bookings"]) == 2


async def test_multiple_bookings_with_matching_hint_disambiguates_and_cancels():
    bookings = [
        BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z"),
        BookingSummary(booking_id="b2", start_iso="2026-07-25T14:00:00.000Z"),
    ]
    provider = _FakeProvider(bookings=bookings)
    executor = CancelAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", requested_datetime_hint="2026-07-25T14:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["cancelled"] is True
    assert result.payload["booking_id"] == "b2"
    assert provider.cancelled_booking_id == "b2"


async def test_hint_matching_nothing_falls_back_to_asking():
    bookings = [BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z")]
    executor = CancelAppointmentExecutor(_FakeProvider(bookings=bookings))
    result = await executor.execute(_request(
        attendee_phone="+14155551234", requested_datetime_hint="2026-08-01T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["cancelled"] is False
    assert result.payload["reason"] == "multiple_bookings_found"


async def test_find_upcoming_bookings_provider_error_is_failed():
    provider = _FakeProvider(find_raises=CalendarProviderError("down"))
    executor = CancelAppointmentExecutor(provider)
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"


async def test_cancel_appointment_provider_error_is_failed():
    booking = BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], cancel_raises=CalendarProviderError("down"))
    executor = CancelAppointmentExecutor(provider)
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"
