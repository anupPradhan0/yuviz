"""
CancelAppointmentExecutor — the only IToolExecutor the LLM's
cancel_appointment tool call ever reaches. Resolves "cancel my appointment"
into a concrete booking via a caller-stated phone number (the same trust
model book_appointment's ANI-based identity uses — see interface.py's
module docstring) rather than exposing a booking_id/uid the LLM never
actually has.

Deliberately NEVER uses ToolExecutionContext.caller_number (the live
call's ANI) even though it's available here too — a caller phoning in to
cancel may not be calling from the same number they originally booked
with (explicitly raised and agreed 2026-07-27), so this always asks the
caller to state a phone number rather than silently trusting the ANI the
way book_appointment does.

Every exit point returns a ToolResult; nothing here raises out to the
middleware chain except a genuinely unexpected bug. Ambiguity (more than
one upcoming booking) is resolved by asking, never by guessing — same
posture as CalendarExecutor's "not available" -> alternatives flow.
"""

from __future__ import annotations

import logging

from ..providers.calendar.interface import CalendarProviderError, ICalendarProvider
from ..types import ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)


class CancelAppointmentExecutor:
    def __init__(self, provider: ICalendarProvider) -> None:
        self._provider = provider

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        args = request.arguments

        attendee_phone = (args.get("attendee_phone") or "").strip()
        if not attendee_phone:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["attendee_phone"]})

        try:
            bookings = await self._provider.find_upcoming_bookings(attendee_phone)
        except CalendarProviderError:
            log.exception("CancelAppointmentExecutor: find_upcoming_bookings failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        if not bookings:
            return ToolResult(status=ToolStatus.SUCCESS, payload={"cancelled": False, "reason": "no_upcoming_booking"})

        hint = (args.get("requested_datetime_hint") or "")[:10]
        candidates = [b for b in bookings if b.start_iso[:10] == hint] if hint else bookings

        if len(candidates) != 1:
            # Zero or multiple matches for the hint (or no hint given and
            # more than one upcoming booking exists) — ask, don't guess.
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "cancelled": False,
                "reason": "multiple_bookings_found",
                "upcoming_bookings": [{"booking_id": b.booking_id, "start": b.start_iso} for b in bookings],
            })

        target = candidates[0]
        try:
            await self._provider.cancel_appointment(target.booking_id, reason="Cancelled by caller via voice")
        except CalendarProviderError:
            log.exception("CancelAppointmentExecutor: cancel_appointment failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        return ToolResult(status=ToolStatus.SUCCESS, payload={
            "cancelled": True, "booking_id": target.booking_id, "cancelled_slot": target.start_iso,
        })
