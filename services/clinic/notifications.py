"""Patient SMS texts and the outbox writes that queue SMS and calendar syncs.

Queued inside the booking's own transaction: the side effect happens if and only if the
booking change committed, and a Google or Twilio outage only delays it.
"""

from __future__ import annotations

import datetime
from typing import Any

from . import speech

EVENTS = ("booked", "rescheduled", "cancelled", "reminder")


def _short_when(moment: datetime.datetime, language: str) -> str:
    if language == "hi":
        return f"{moment.day} {speech._HI_MONTHS[moment.month - 1]}, {speech.spoken_time(moment, 'hi')}"
    return f"{moment:%a} {moment.day} {moment:%b}, {speech.spoken_time(moment, 'en')}"


def sms_text(
    event: str, *, settings: dict, appointment: dict, doctor: dict, department: dict,
    local_start: datetime.datetime,
) -> str:
    language = speech.lang_or_default(appointment.get("language"))
    hospital = (settings.get("hospital_name_hi") if language == "hi" else None) or settings["hospital_name"]
    who = speech.doctor_name(doctor, language)
    dept = speech.department_name(department, language)
    when = _short_when(local_start, language)
    ref = appointment["reference"]
    phone = settings.get("hospital_phone")
    if language == "hi":
        call_us = f" बदलाव के लिए कॉल करें: {phone}" if phone else ""
        texts = {
            "booked": f"{hospital}: {who} ({dept}) के साथ आपका अपॉइंटमेंट {when} पक्का है। "
                      f"बुकिंग नं. {ref}। कृपया 15 मिनट पहले पहुँचें।{call_us}",
            "rescheduled": f"{hospital}: आपका अपॉइंटमेंट (बुकिंग नं. {ref}) अब {who} के साथ {when} है।{call_us}",
            "cancelled": f"{hospital}: {who} के साथ {when} का आपका अपॉइंटमेंट (बुकिंग नं. {ref}) रद्द कर दिया गया है।",
            "reminder": f"{hospital}: याद दिलाना — {who} ({dept}) के साथ आपका अपॉइंटमेंट {when} है। "
                        f"बुकिंग नं. {ref}।{call_us}",
        }
    else:
        call_us = f" To change it, call {phone}." if phone else ""
        texts = {
            "booked": f"{hospital}: Appointment confirmed with {who} ({dept}) on {when}. "
                      f"Booking no. {ref}. Please arrive 15 min early.{call_us}",
            "rescheduled": f"{hospital}: Your appointment (booking no. {ref}) is now with {who} on {when}.{call_us}",
            "cancelled": f"{hospital}: Your appointment with {who} on {when} (booking no. {ref}) is cancelled.",
            "reminder": f"{hospital}: Reminder - your appointment with {who} ({dept}) is on {when}. "
                        f"Booking no. {ref}.{call_us}",
        }
    return texts[event]


async def queue_sms(
    conn: Any, *, settings: dict, appointment: dict, doctor: dict, department: dict,
    local_start: datetime.datetime, event: str,
) -> None:
    if not settings.get("sms_enabled"):
        return
    body = sms_text(
        event, settings=settings, appointment=appointment, doctor=doctor, department=department,
        local_start=local_start,
    )
    # The start time is in the key: a second reschedule is a second message, a retried
    # request for the same change is not.
    dedupe_key = f"sms:{appointment['id']}:{event}:{int(local_start.timestamp())}"
    await conn.execute(
        "INSERT INTO clinic_outbox (tenant_id, appointment_id, kind, payload, dedupe_key) "
        "VALUES ($1, $2, 'sms', jsonb_build_object('to', $3::text, 'body', $4::text, 'event', $5::text), $6) "
        "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING",
        appointment["tenant_id"], appointment["id"], appointment["patient_phone"], body, event, dedupe_key,
    )


async def queue_calendar_sync(conn: Any, *, settings: dict, appointment: dict) -> None:
    """One row per appointment, re-armed on every change: the worker makes Google match
    the appointment's current state, so the order of changes cannot matter."""
    if not settings.get("google_connection_id"):
        return
    await conn.execute(
        "INSERT INTO clinic_outbox (tenant_id, appointment_id, kind, dedupe_key) "
        "VALUES ($1, $2, 'gcal_sync', $3) "
        "ON CONFLICT (tenant_id, dedupe_key) DO UPDATE "
        "SET rev = clinic_outbox.rev + 1, status = 'pending', attempts = 0, "
        "    next_attempt_at = now(), last_error = NULL, done_at = NULL",
        appointment["tenant_id"], appointment["id"], f"gcal:{appointment['id']}",
    )
