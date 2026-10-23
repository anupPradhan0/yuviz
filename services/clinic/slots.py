"""The slot engine: weekly OPD hours minus leave, bookings and other callers' holds.

Pure functions over plain values, so availability can be tested without a database.
All datetimes passed in are timezone-aware; the grid is laid out in the clinic's timezone.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

Interval = tuple[datetime.datetime, datetime.datetime]


@dataclass(frozen=True)
class ScheduleBlock:
    weekday: int                       # 0 = Monday
    start_time: datetime.time
    end_time: datetime.time
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None

    def applies_on(self, day: datetime.date) -> bool:
        if day.weekday() != self.weekday:
            return False
        if self.valid_from is not None and day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


def _overlaps(start: datetime.datetime, end: datetime.datetime, intervals: Iterable[Interval]) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in intervals)


def slot_grid(
    blocks: Iterable[ScheduleBlock], day: datetime.date, slot_minutes: int, tz: ZoneInfo,
) -> list[Interval]:
    """Every slot the doctor's OPD hours define on `day`, in order, with no partial slot at the end."""
    step = datetime.timedelta(minutes=slot_minutes)
    slots: list[Interval] = []
    for block in sorted((b for b in blocks if b.applies_on(day)), key=lambda b: b.start_time):
        cursor = datetime.datetime.combine(day, block.start_time, tzinfo=tz)
        block_end = datetime.datetime.combine(day, block.end_time, tzinfo=tz)
        while cursor + step <= block_end:
            slots.append((cursor, cursor + step))
            cursor += step
    return slots


def free_slots(
    *,
    blocks: Iterable[ScheduleBlock],
    slot_minutes: int,
    tz: ZoneInfo,
    first_day: datetime.date,
    last_day: datetime.date,
    not_before: datetime.datetime,
    unavailable: Iterable[Interval],
) -> list[Interval]:
    """Free slots from first_day to last_day inclusive. `unavailable` is leave, live
    bookings and other callers' unexpired holds; `not_before` is now + the minimum lead time."""
    blocks = list(blocks)
    unavailable = list(unavailable)
    free: list[Interval] = []
    day = first_day
    while day <= last_day:
        for start, end in slot_grid(blocks, day, slot_minutes, tz):
            if start >= not_before and not _overlaps(start, end, unavailable):
                free.append((start, end))
        day += datetime.timedelta(days=1)
    return free


def part_of_day(moment: datetime.datetime) -> str:
    if moment.hour < 12:
        return "morning"
    if moment.hour < 16:
        return "afternoon"
    return "evening"


def pick_for_offer(slots: list[Interval], *, limit: int, part: str | None = None) -> list[Interval]:
    """What to read out: the earliest `limit` slots, but never more than two from one
    session (morning/afternoon/evening) of one day while later days have room, so the
    caller hears a real choice rather than 10:00, 10:15, 10:30 and 10:45."""
    if part:
        slots = [s for s in slots if part_of_day(s[0]) == part]
    chosen: list[Interval] = []
    per_session: dict[tuple[datetime.date, str], int] = {}
    for start, end in slots:
        key = (start.date(), part_of_day(start))
        if per_session.get(key, 0) >= 2:
            continue
        per_session[key] = per_session.get(key, 0) + 1
        chosen.append((start, end))
        if len(chosen) >= limit:
            return chosen
    # Not enough variety: fill with whatever is earliest.
    for slot in slots:
        if len(chosen) >= limit:
            break
        if slot not in chosen:
            chosen.append(slot)
    return sorted(chosen)
