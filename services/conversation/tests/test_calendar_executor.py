"""
CalendarExecutor tests — pure unit tests against a fake ICalendarProvider,
no network at all. Covers every branch in the design's §12 flowchart, plus
the phone-first identity behavior added 2026-07-27: the live call's own
ANI (ToolExecutionContext.caller_number) is used automatically when
present, and the attendee_phone tool argument is only a fallback for
sessions with no ANI at all (e.g. a webcall/browser test).
"""

from __future__ import annotations

from services.conversation.tools.executors.calendar_executor import CalendarExecutor
from services.conversation.tools.providers.calendar.interface import (
    AttendeeInfo, AvailabilitySlot, BookingResult, CalendarProviderError,
    InvalidAttendeePhoneError, SlotUnavailableError,
)
from services.conversation.tools.types import ToolExecutionContext, ToolExecutionRequest, ToolStatus


def _ctx(caller_number: str = "") -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1",
        turn_id="turn1", tool_iteration=0, deadline=0.0, request_id="r1",
        caller_number=caller_number,
    )


def _request(*, caller_number: str = "", **args) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_call_id="call1", tool_name="book_appointment", arguments=args, context=_ctx(caller_number),
    )


class _FakeProvider:
    def __init__(
        self, *, available: bool = True, requires_phone: bool = False, default_phone: str | None = None,
        book_raises: Exception | None = None, check_raises: Exception | None = None,
        slots_raises: Exception | None = None, slots: list[AvailabilitySlot] | None = None,
        default_timezone: str = "UTC",
    ) -> None:
        self._available = available
        self._requires_phone = requires_phone
        self._default_phone = default_phone
        self._book_raises = book_raises
        self._check_raises = check_raises
        self._slots_raises = slots_raises
        self._slots = slots if slots is not None else [AvailabilitySlot(start_iso="2026-07-24T10:00:00.000Z")]
        self._default_timezone = default_timezone
        self.booked_with: AttendeeInfo | None = None

    def requires_attendee_phone(self) -> bool:
        return self._requires_phone

    @property
    def default_attendee_phone(self) -> str | None:
        return self._default_phone

    @property
    def default_timezone(self) -> str:
        return self._default_timezone

    async def check_availability(self, requested_datetime, timezone):
        if self._check_raises:
            raise self._check_raises
        return self._available

    async def book_appointment(self, slot_iso, attendee, notes=""):
        if self._book_raises:
            raise self._book_raises
        self.booked_with = attendee
        return BookingResult(booking_id="b1", confirmed_slot=slot_iso, meeting_url="https://example.com/m1")

    async def find_available_slots(self, near_datetime, timezone, limit=3):
        if self._slots_raises:
            raise self._slots_raises
        return self._slots

    async def cancel_appointment(self, booking_id, reason=""):
        raise NotImplementedError


async def test_missing_requested_datetime_is_invalid_argument():
    executor = CalendarExecutor(_FakeProvider())
    result = await executor.execute(_request(attendee_name="Jane"))

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["requested_datetime"]


async def test_missing_required_phone_with_no_ani_or_default_is_invalid_argument():
    executor = CalendarExecutor(_FakeProvider(requires_phone=True, default_phone=None))
    result = await executor.execute(_request(requested_datetime="2026-07-24T10:00:00.000Z"))

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["attendee_phone"]


async def test_missing_phone_but_default_configured_proceeds_without_asking():
    provider = _FakeProvider(requires_phone=True, default_phone="+14155550000", available=True)
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(requested_datetime="2026-07-24T10:00:00.000Z"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["booked"] is True


async def test_live_call_ani_is_used_automatically_without_asking():
    """The whole point of the 2026-07-27 redesign: a real telephony call
    never needs to ask for anything — the ANI is enough."""
    provider = _FakeProvider(requires_phone=True, default_phone=None, available=True)
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z", caller_number="+14155551234",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert provider.booked_with.phone == "+14155551234"


async def test_explicit_attendee_phone_wins_over_ani():
    """Found live 2026-07-30: an ANI that's already been rejected once
    (InvalidAttendeePhoneError) must not keep winning on the retry once the
    caller has explicitly stated a different, real number — the whole
    retry path is pointless otherwise. A non-empty attendee_phone tool
    argument always overrides the ANI, never the reverse."""
    provider = _FakeProvider(available=True)
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z",
        caller_number="1001",  # the extension/ANI that was just rejected
        attendee_phone="+918971188211",  # what the caller just stated instead
    ))

    assert result.status == ToolStatus.SUCCESS
    assert provider.booked_with.phone == "+918971188211"


async def test_available_slot_books_successfully():
    executor = CalendarExecutor(_FakeProvider(available=True))
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z", attendee_name="Jane", caller_number="+14155551234",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["booked"] is True
    assert result.payload["booking_id"] == "b1"
    assert result.payload["meeting_url"] == "https://example.com/m1"


async def test_unavailable_slot_returns_success_with_alternatives():
    executor = CalendarExecutor(_FakeProvider(available=False))
    result = await executor.execute(_request(requested_datetime="2026-07-24T17:00:00.000Z"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["booked"] is False
    assert result.payload["available_slots"] == ["2026-07-24T10:00:00.000Z"]


async def test_booking_time_conflict_falls_through_to_alternatives_not_failed():
    provider = _FakeProvider(available=True, book_raises=SlotUnavailableError("taken"))
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z", caller_number="+14155551234",
    ))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["booked"] is False
    assert result.payload["available_slots"] == ["2026-07-24T10:00:00.000Z"]


async def test_book_appointment_invalid_phone_asks_for_a_different_number_not_failed():
    """The 2026-07-29 fix: an ANI that Cal.com rejects as not a real phone
    number (e.g. a 4-digit internal test extension) must not collapse into
    a generic FAILED — it's recoverable, same shape as "no phone at all"
    (missing_fields=[attendee_phone]) but with a reason distinguishing
    "this one was rejected" from "we never had one", so the agent asks for
    a DIFFERENT number instead of sounding like it's asking for the first
    time."""
    provider = _FakeProvider(available=True, book_raises=InvalidAttendeePhoneError("invalid_number"))
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z", caller_number="1001",
    ))

    assert result.status == ToolStatus.INVALID_ARGUMENT
    assert result.payload["missing_fields"] == ["attendee_phone"]
    assert result.payload["reason"] == "invalid_phone_number"


async def test_check_availability_provider_error_is_failed():
    provider = _FakeProvider(check_raises=CalendarProviderError("down"))
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(requested_datetime="2026-07-24T10:00:00.000Z"))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"


async def test_book_appointment_provider_error_is_failed():
    provider = _FakeProvider(available=True, book_raises=CalendarProviderError("down"))
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(
        requested_datetime="2026-07-24T10:00:00.000Z", caller_number="+14155551234",
    ))

    assert result.status == ToolStatus.FAILED
    assert result.error == "calendar_error"


async def test_find_available_slots_failure_after_unavailable_degrades_to_empty_list_not_failed():
    provider = _FakeProvider(available=False, slots_raises=CalendarProviderError("down"))
    executor = CalendarExecutor(provider)
    result = await executor.execute(_request(requested_datetime="2026-07-24T17:00:00.000Z"))

    assert result.status == ToolStatus.SUCCESS
    assert result.payload["booked"] is False
    assert result.payload["available_slots"] == []
