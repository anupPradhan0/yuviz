"""
RescheduleAppointmentExecutor — the only IToolExecutor the LLM's
reschedule_appointment tool call ever reaches. Resolves "reschedule my
appointment" the same way CancelAppointmentExecutor resolves "cancel my
appointment" (find_upcoming_bookings by a caller-stated phone number,
disambiguate via an optional date hint — see that executor's docstring for
why this deliberately never uses the live call's ANI), then moves it via
ICalendarProvider.reschedule_appointment(), which is one atomic
provider-side call, not cancel()+book_appointment() composed here (see
providers/calendar/interface.py's module docstring for why that's safe).

Every exit point returns a ToolResult; nothing here raises out to the
middleware chain except a genuinely unexpected bug.
"""

from __future__ import annotations

import logging

from ..providers.calendar.interface import CalendarProviderError, ICalendarProvider, SlotUnavailableError
from ..types import ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)


class RescheduleAppointmentExecutor:
    def __init__(self, provider: ICalendarProvider) -> None:
        self._provider = provider

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        args = request.arguments

        attendee_phone = (args.get("attendee_phone") or "").strip()
        if not attendee_phone:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["attendee_phone"]})

        new_datetime = args.get("new_requested_datetime")
        if not new_datetime:
            return ToolResult(
                status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["new_requested_datetime"]},
            )

        try:
            bookings = await self._provider.find_upcoming_bookings(attendee_phone)
        except CalendarProviderError:
            log.exception(
                "RescheduleAppointmentExecutor: find_upcoming_bookings failed call_id=%s", request.tool_call_id,
            )
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        if not bookings:
            return ToolResult(status=ToolStatus.SUCCESS, payload={"rescheduled": False, "reason": "no_upcoming_booking"})

        hint = (args.get("requested_datetime_hint") or "")[:10]
        candidates = [b for b in bookings if b.start_iso[:10] == hint] if hint else bookings

        if len(candidates) != 1:
            # Zero or multiple matches for the hint (or no hint given and
            # more than one upcoming booking exists) — ask, don't guess.
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "rescheduled": False,
                "reason": "multiple_bookings_found",
                "upcoming_bookings": [{"booking_id": b.booking_id, "start": b.start_iso} for b in bookings],
            })

        target = candidates[0]
        # Always the business's own timezone, never a caller-guessed one —
        # see CalendarExecutor's identical fix for why.
        tz = self._provider.default_timezone

        try:
            available = await self._provider.check_availability(new_datetime, tz)
        except CalendarProviderError:
            log.exception(
                "RescheduleAppointmentExecutor: check_availability failed call_id=%s", request.tool_call_id,
            )
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        if available:
            try:
                booking = await self._provider.reschedule_appointment(target.booking_id, new_datetime, tz)
                return ToolResult(status=ToolStatus.SUCCESS, payload={
                    "rescheduled": True,
                    "old_slot": target.start_iso,
                    "new_slot": booking.confirmed_slot,
                    "booking_id": booking.booking_id,
                    "meeting_url": booking.meeting_url,
                })
            except SlotUnavailableError:
                # Race: check_availability said yes, but the slot was taken
                # before reschedule() landed — same posture as
                # CalendarExecutor's own booking-time-conflict handling.
                log.info("RescheduleAppointmentExecutor: reschedule conflict (race) call_id=%s", request.tool_call_id)
            except CalendarProviderError:
                log.exception(
                    "RescheduleAppointmentExecutor: reschedule_appointment failed call_id=%s", request.tool_call_id,
                )
                return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        try:
            slots = await self._provider.find_available_slots(new_datetime, tz)
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "rescheduled": False,
                "reason": "requested_time_unavailable",
                "available_slots": [s.start_iso for s in slots],
            })
        except CalendarProviderError:
            log.exception(
                "RescheduleAppointmentExecutor: find_available_slots failed call_id=%s", request.tool_call_id,
            )
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "rescheduled": False, "reason": "requested_time_unavailable", "available_slots": [],
            })
