"""
RescheduleAppointmentExecutor tests — pure unit tests against a fake
ICalendarProvider, no network at all.
"""

from __future__ import annotations

from services.conversation.tools.executors.reschedule_appointment_executor import RescheduleAppointmentExecutor
from services.conversation.tools.providers.calendar.interface import (
    BookingResult, BookingSummary, CalendarProviderError, SlotUnavailableError,
)
from services.conversation.tools.types import ToolExecutionContext, ToolExecutionRequest, ToolStatus


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1",
        turn_id="turn1", tool_iteration=0, deadline=0.0, request_id="r1",
    )


def _request(**args) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_call_id="call1", tool_name="reschedule_appointment", arguments=args, context=_ctx(),
    )


class _FakeProvider:
    def __init__(
        self, *, bookings: list[BookingSummary] | None = None, available: bool = True,
        find_raises: Exception | None = None, check_raises: Exception | None = None,
        reschedule_raises: Exception | None = None, slots: list | None = None,
    ) -> None:
        self._bookings = bookings if bookings is not None else []
        self._available = available
        self._find_raises = find_raises
        self._check_raises = check_raises
        self._reschedule_raises = reschedule_raises
        self._slots = slots if slots is not None else []
        self.rescheduled_booking_id: str | None = None
        self.rescheduled_to: str | None = None

    async def find_upcoming_bookings(self, attendee_phone, limit=5):
        if self._find_raises:
            raise self._find_raises
        return self._bookings

    async def check_availability(self, requested_datetime, timezone):
        if self._check_raises:
            raise self._check_raises
        return self._available

    async def reschedule_appointment(self, booking_id, new_slot_iso, timezone):
        if self._reschedule_raises:
            raise self._reschedule_raises
        self.rescheduled_booking_id = booking_id
        self.rescheduled_to = new_slot_iso
        return BookingResult(booking_id="newUid", confirmed_slot=new_slot_iso, meeting_url="https://example.com/m")

    async def find_available_slots(self, near_datetime, timezone, limit=3):
        return self._slots

    @property
    def default_timezone(self) -> str:
        return "UTC"


async def test_missing_attendee_phone_is_invalid_argument():
    executor = RescheduleAppointmentExecutor(_FakeProvider())
    result = await executor.execute(_request(new_requested_datetime="2026-07-28T10:00:00.000Z"))

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["attendee_phone"]


async def test_missing_new_datetime_is_invalid_argument():
    executor = RescheduleAppointmentExecutor(_FakeProvider())
    result = await executor.execute(_request(attendee_phone="+14155551234"))

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["new_requested_datetime"]


async def test_no_upcoming_booking_is_success_not_failed():
    executor = RescheduleAppointmentExecutor(_FakeProvider(bookings=[]))
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload == {"rescheduled": False, "reason": "no_upcoming_booking"}


async def test_single_booking_and_available_slot_reschedules():
    booking = BookingSummary(booking_id="old1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], available=True)
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["rescheduled"] is True
    assert result.payload["new_slot"] == "2026-07-28T10:00:00.000Z"
    assert result.payload["booking_id"] == "newUid"
    assert provider.rescheduled_booking_id == "old1"
    assert provider.rescheduled_to == "2026-07-28T10:00:00.000Z"


async def test_multiple_bookings_with_no_hint_asks_instead_of_guessing():
    bookings = [
        BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z"),
        BookingSummary(booking_id="b2", start_iso="2026-07-25T14:00:00.000Z"),
    ]
    executor = RescheduleAppointmentExecutor(_FakeProvider(bookings=bookings))
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["rescheduled"] is False
    assert result.payload["reason"] == "multiple_bookings_found"
    assert len(result.payload["upcoming_bookings"]) == 2


async def test_multiple_bookings_with_matching_hint_disambiguates_and_reschedules():
    bookings = [
        BookingSummary(booking_id="b1", start_iso="2026-07-24T10:00:00.000Z"),
        BookingSummary(booking_id="b2", start_iso="2026-07-25T14:00:00.000Z"),
    ]
    provider = _FakeProvider(bookings=bookings, available=True)
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
        requested_datetime_hint="2026-07-25T14:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["rescheduled"] is True
    assert provider.rescheduled_booking_id == "b2"


async def test_new_time_unavailable_returns_alternatives_not_failed():
    booking = BookingSummary(booking_id="old1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], available=False, slots=[])
    from services.conversation.tools.providers.calendar.interface import AvailabilitySlot
    provider._slots = [AvailabilitySlot(start_iso="2026-07-29T10:00:00.000Z")]
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["rescheduled"] is False
    assert result.payload["reason"] == "requested_time_unavailable"
    assert result.payload["available_slots"] == ["2026-07-29T10:00:00.000Z"]
    assert provider.rescheduled_booking_id is None


async def test_reschedule_conflict_race_falls_through_to_alternatives_not_failed():
    booking = BookingSummary(booking_id="old1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], available=True, reschedule_raises=SlotUnavailableError("taken"))
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["rescheduled"] is False
    assert result.payload["reason"] == "requested_time_unavailable"


async def test_find_upcoming_bookings_provider_error_is_failed():
    provider = _FakeProvider(find_raises=CalendarProviderError("down"))
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"


async def test_check_availability_provider_error_is_failed():
    booking = BookingSummary(booking_id="old1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], check_raises=CalendarProviderError("down"))
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"


async def test_reschedule_appointment_provider_error_is_failed():
    booking = BookingSummary(booking_id="old1", start_iso="2026-07-24T10:00:00.000Z")
    provider = _FakeProvider(bookings=[booking], available=True, reschedule_raises=CalendarProviderError("down"))
    executor = RescheduleAppointmentExecutor(provider)
    result = await executor.execute(_request(
        attendee_phone="+14155551234", new_requested_datetime="2026-07-28T10:00:00.000Z",
    ))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"
