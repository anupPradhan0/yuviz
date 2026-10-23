"""Spoken dates and times in English and Hindi.

Everything the agent reads aloud about a booking is rendered here, from server data,
so the caller hears exactly what is stored — never the model's paraphrase of it.
"""

from __future__ import annotations

import datetime

LANGUAGES = ("en", "hi")

_HI_WEEKDAYS = ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार")
_HI_MONTHS = (
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
)


def lang_or_default(language: str | None) -> str:
    return language if language in LANGUAGES else "en"


def _clock(moment: datetime.datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}" if moment.minute == 0 else f"{hour}:{moment.minute:02d}"


def spoken_time(moment: datetime.datetime, language: str = "en") -> str:
    """'11:30 AM' / 'सुबह 11:30 बजे'. `moment` must already be in the clinic's timezone."""
    if lang_or_default(language) == "hi":
        hour = moment.hour
        if hour < 12:
            part = "सुबह"
        elif hour < 16:
            part = "दोपहर"
        elif hour < 19:
            part = "शाम"
        else:
            part = "रात"
        return f"{part} {_clock(moment)} बजे"
    return f"{_clock(moment)} {'AM' if moment.hour < 12 else 'PM'}"


def spoken_date(day: datetime.date, today: datetime.date, language: str = "en") -> str:
    """'today' / 'tomorrow' / 'Monday, 20 October' — and the Hindi equivalents."""
    delta = (day - today).days
    if lang_or_default(language) == "hi":
        if delta == 0:
            return "आज"
        if delta == 1:
            return "कल"
        if delta == 2:
            return "परसों"
        return f"{_HI_WEEKDAYS[day.weekday()]}, {day.day} {_HI_MONTHS[day.month - 1]}"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"{day:%A}, {day.day} {day:%B}"


def spoken_when(moment: datetime.datetime, today: datetime.date, language: str = "en") -> str:
    """'tomorrow at 11:30 AM' / 'कल सुबह 11:30 बजे'."""
    if lang_or_default(language) == "hi":
        return f"{spoken_date(moment.date(), today, 'hi')} {spoken_time(moment, 'hi')}"
    return f"{spoken_date(moment.date(), today, 'en')} at {spoken_time(moment, 'en')}"


def spoken_reference(reference: str) -> str:
    """'482913' -> '4 8 2 9 1 3': digits read one by one, in either language."""
    return " ".join(reference)


def doctor_name(doctor: dict, language: str = "en") -> str:
    if lang_or_default(language) == "hi" and doctor.get("name_hi"):
        return doctor["name_hi"]
    return doctor["name"]


def department_name(department: dict, language: str = "en") -> str:
    if lang_or_default(language) == "hi" and department.get("name_hi"):
        return department["name_hi"]
    return department["name"]
