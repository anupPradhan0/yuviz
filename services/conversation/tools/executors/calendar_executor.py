"""
CalendarExecutor — the only IToolExecutor the LLM's book_appointment tool
call ever reaches. Availability checking, alternative-slot lookup, and the
actual booking call are all internal to execute() — ToolCallOrchestrator
sees exactly one execute(request) -> ToolResult call, identical in shape to
any other tool (see Tool Execution Framework design §12).

Every exit point returns a ToolResult; nothing here raises out to the
middleware chain except a genuinely unexpected bug.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..providers.calendar.interface import (
    AttendeeInfo, CalendarProviderError, ICalendarProvider, InvalidAttendeePhoneError, SlotUnavailableError,
)
from ..providers.sms.interface import ISmsProvider, SmsProviderError
from ..types import ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)


def _format_when(requested_datetime: str) -> str:
    """requested_datetime is the same naive, business-local wall-clock
    string that was actually booked (see book_appointment's own schema
    description) — formatting it directly, rather than re-deriving a
    human-readable time from Cal.com's returned UTC confirmed_slot, avoids
    a second timezone conversion that could introduce its own bug."""
    try:
        dt = datetime.fromisoformat(requested_datetime)
        return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
    except ValueError:
        return requested_datetime


def _speak_confirmation(requested_datetime: str, sms_sent: bool = False) -> str:
    base = f"You're all set — I've booked your appointment for {_format_when(requested_datetime)}."
    if sms_sent:
        # Only ever said when the send actually succeeded (see
        # _send_confirmation_sms's return value) — never assumed, same
        # discipline as every other deterministic_response in this
        # codebase: the caller must never be told something happened that
        # didn't actually happen.
        base += " I've also sent a confirmation text to your number."
    return base


def _tz_abbreviation(requested_datetime: str, tz: str) -> str:
    """A texted confirmation is read later, out of the live call's own
    context, unlike the spoken one — spelling out the timezone (IST, not
    just a bare time) avoids ambiguity for whoever reads it back. tz is
    the same provider.default_timezone already used to book the slot
    (see execute()) — an IANA name like "Asia/Kolkata", not the caller's
    own timezone, which was never relevant to begin with (see
    book_appointment's requested_datetime schema)."""
    try:
        dt = datetime.fromisoformat(requested_datetime).replace(tzinfo=ZoneInfo(tz))
        return dt.strftime("%Z")
    except (ValueError, KeyError):
        return ""


def _sms_confirmation(requested_datetime: str, tz: str) -> str:
    when = _format_when(requested_datetime)
    abbr = _tz_abbreviation(requested_datetime, tz)
    if abbr:
        when = f"{when} {abbr}"
    return f"Your appointment is confirmed for {when}. See you then!"


class CalendarExecutor:
    def __init__(self, provider: ICalendarProvider, sms_provider: ISmsProvider | None = None) -> None:
        self._provider = provider
        self._sms_provider = sms_provider

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        args = request.arguments

        requested_datetime = args.get("requested_datetime")
        if not requested_datetime:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["requested_datetime"]})

        # requested_datetime is always in the business's own local time (see
        # the tool schema) — never the caller's. An in-person appointment at
        # a single physical location happens in that location's timezone
        # regardless of where the caller is phoning from; letting the LLM
        # supply a caller-guessed timezone here caused every non-local-tz
        # caller's booking to silently miscompare against real Cal.com slots
        # and never post (found live 2026-08-01).
        tz = self._provider.default_timezone

        # A real telephony call always has a caller_number (ANI) — used
        # automatically so the agent never has to ask up front. BUT: an
        # explicit attendee_phone argument, when present, must win over the
        # ANI, not the other way around — the LLM only ever populates that
        # argument when it was told to ask (no ANI at all, or the ANI was
        # just rejected by InvalidAttendeePhoneError below). Found live
        # 2026-07-30: with the old (ani or args) priority, every retry
        # after an invalid-ANI rejection silently kept resending the same
        # already-rejected ANI, ignoring whatever real number the caller
        # had just stated — the retry path could never actually succeed.
        attendee_phone = (args.get("attendee_phone") or request.context.caller_number or "").strip()

        # Deterministic gate, not just a prompt instruction: confirmed
        # live that the LLM can silently skip the phone-confirmation step
        # entirely (the caller changed the subject instead of answering,
        # and the model just proceeded to book anyway against the
        # unconfirmed ANI). Only applies when attendee_phone came from the
        # ANI, not an explicit attendee_phone argument — an explicit
        # argument means the caller just stated a number THIS turn, a
        # different case with its own digit-confirmation guardrail in the
        # system prompt, not this ANI-specific one.
        if (
            not args.get("attendee_phone")
            and request.context.caller_number
            and not request.context.phone_number_confirmed
        ):
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                "missing_fields": ["attendee_phone"],
                "reason": "phone_not_confirmed",
            })

        # Business-rule-level validation: only ask the caller when the
        # provider genuinely requires a phone number AND no tenant-
        # configured default exists AND we don't already have the ANI.
        if (
            self._provider.requires_attendee_phone()
            and not attendee_phone
            and not self._provider.default_attendee_phone
        ):
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["attendee_phone"]})

        attendee = AttendeeInfo(
            name=args.get("attendee_name") or "Caller",
            phone=attendee_phone,
            timezone=tz,
        )

        try:
            available = await self._provider.check_availability(requested_datetime, tz)
        except CalendarProviderError:
            log.exception("CalendarExecutor: check_availability failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        if available:
            try:
                booking = await self._provider.book_appointment(
                    requested_datetime, attendee, notes=args.get("notes", ""),
                )
                sms_sent = await self._send_confirmation_sms(
                    attendee.phone, requested_datetime, tz, request.tool_call_id,
                )
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    payload={
                        "booked": True,
                        "booking_id": booking.booking_id,
                        "confirmed_slot": booking.confirmed_slot,
                        "meeting_url": booking.meeting_url,
                    },
                    deterministic_response=_speak_confirmation(requested_datetime, sms_sent=sms_sent),
                    confirmed_datetime=requested_datetime,
                )
            except SlotUnavailableError:
                # Race: check_availability said yes, but the slot was taken
                # before book() landed — treated identically to an upfront
                # "not available" (see design §12), not FAILED.
                log.info("CalendarExecutor: booking conflict (race) call_id=%s", request.tool_call_id)
            except InvalidAttendeePhoneError:
                # The ANI (or a caller-stated number) reached Cal.com fine
                # but failed ITS phone-number validation — e.g. a caller ID
                # that isn't a real phone number at all (confirmed live
                # 2026-07-29 with a SIP test extension). Distinct from
                # "no phone number at all": same recovery shape
                # (missing_fields asks the LLM to collect attendee_phone
                # and retry) but with a reason so the agent doesn't sound
                # like it's asking for a phone number for the first time —
                # it's telling the caller the one on file didn't work.
                log.info(
                    "CalendarExecutor: attendee phone rejected as invalid call_id=%s", request.tool_call_id,
                )
                return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                    "missing_fields": ["attendee_phone"],
                    "reason": "invalid_phone_number",
                })
            except CalendarProviderError:
                log.exception("CalendarExecutor: book_appointment failed call_id=%s", request.tool_call_id)
                return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        try:
            slots = await self._provider.find_available_slots(requested_datetime, tz)
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "booked": False,
                "available_slots": [s.start_iso for s in slots],
            })
        except CalendarProviderError:
            # The core "not available" answer is still valid business data
            # even if alternatives couldn't be fetched — degrade to an
            # empty list, not FAILED (see design §12).
            log.exception("CalendarExecutor: find_available_slots failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.SUCCESS, payload={"booked": False, "available_slots": []})

    async def _send_confirmation_sms(
        self, to_number: str, requested_datetime: str, tz: str, tool_call_id: str,
    ) -> bool:
        # Best-effort, never affects the booking's own ToolResult — see
        # ISmsProvider's module docstring for why a text-send failure must
        # never look like a booking failure to the caller. No provider
        # configured (self._sms_provider is None) and no phone number to
        # text (a webcall/browser test with no ANI) both just skip
        # silently, same "feature simply off" posture as the rest of this
        # executor's optional behavior. Return value feeds
        # _speak_confirmation's sms_sent flag — the caller must only ever
        # be told a text was sent when one actually was.
        if self._sms_provider is None or not to_number:
            return False
        try:
            await self._sms_provider.send_sms(to_number, _sms_confirmation(requested_datetime, tz))
            return True
        except SmsProviderError:
            log.exception("CalendarExecutor: confirmation SMS failed call_id=%s", tool_call_id)
            return False
