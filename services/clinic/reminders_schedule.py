"""When to place an appointment's reminder call. Pure, so the rules are testable."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

# A reminder this close to the appointment is no longer useful; skip it.
_MIN_GAP_BEFORE_APPOINTMENT = datetime.timedelta(hours=2)
_SOONEST = datetime.timedelta(minutes=5)


def _window_bounds(day: datetime.date, settings: dict, tz: ZoneInfo) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime.combine(day, settings["reminder_window_start"], tzinfo=tz)
    end = datetime.datetime.combine(day, settings["reminder_window_end"], tzinfo=tz)
    return start, end


def clamp_into_window(moment: datetime.datetime, settings: dict, tz: ZoneInfo) -> datetime.datetime:
    """Earliest calling-hours moment at or after `moment`."""
    local = moment.astimezone(tz)
    start, end = _window_bounds(local.date(), settings, tz)
    if local < start:
        return start
    if local >= end:
        next_start, _ = _window_bounds(local.date() + datetime.timedelta(days=1), settings, tz)
        return next_start
    return local


def first_reminder_at(
    starts_at: datetime.datetime, now: datetime.datetime, settings: dict, tz: ZoneInfo,
) -> datetime.datetime | None:
    """`reminder_hours_before` ahead of the appointment, moved earlier into calling hours
    if it lands outside them; None when reminders are off or there is no useful time left."""
    if not settings.get("reminder_enabled"):
        return None
    latest_useful = starts_at - _MIN_GAP_BEFORE_APPOINTMENT
    target = (starts_at - datetime.timedelta(hours=settings["reminder_hours_before"])).astimezone(tz)
    start, end = _window_bounds(target.date(), settings, tz)
    if target < start:
        target = start
    elif target >= end:
        target = end - datetime.timedelta(minutes=30)
    if target < now + _SOONEST:
        target = clamp_into_window(now + _SOONEST, settings, tz)
    return target if target <= latest_useful else None


def retry_at(
    now: datetime.datetime, starts_at: datetime.datetime, settings: dict, tz: ZoneInfo,
) -> datetime.datetime | None:
    target = clamp_into_window(now + datetime.timedelta(minutes=settings["reminder_retry_minutes"]), settings, tz)
    return target if target <= starts_at - _MIN_GAP_BEFORE_APPOINTMENT else None
