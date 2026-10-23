"""The agent's clinic tools, called by toolexec as ordinary custom APIs.

The tenant comes from the bearer key (toolauth). `caller_phone` is toolexec's `caller_id`
param source, i.e. the number on the call, never a value the model chose.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from libs.tenancy import tenant_conn

from .. import booking, db, speech
from ..toolauth import require_tool_tenant

router = APIRouter(prefix="/tool/v1", tags=["clinic-tools"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")
    language: str | None = "en"
    caller_phone: str | None = None


class DepartmentsBody(_Body):
    pass


class DoctorsBody(_Body):
    doctor: str | None = None
    department: str | None = None
    consult_language: str | None = None


class AvailabilityBody(_Body):
    doctor: str | None = None
    department: str | None = None
    date: str | None = None
    part_of_day: str | None = None


class PrepareBookingBody(_Body):
    doctor: str = ""
    slot_start: str = ""
    patient_name: str = ""
    patient_phone: str | None = None


class BookBody(_Body):
    doctor_code: str = ""
    slot_start: str = ""
    patient_name: str = ""
    patient_phone: str = ""
    reason: str | None = None
    patient_age: Any = None


class FindBody(_Body):
    reference: str | None = None
    patient_phone: str | None = None


class PrepareRescheduleBody(_Body):
    reference: str = ""
    new_slot_start: str = ""
    patient_phone: str | None = None
    doctor: str | None = None        # prepare: move to another doctor
    doctor_code: str | None = None   # action: the doctor prepare resolved


class CancelBody(_Body):
    reference: str = ""
    patient_phone: str | None = None
    reason: str | None = None


class ReminderOutcomeBody(_Body):
    reference: str = ""
    outcome: str = ""


async def _run(tenant_id: str, work: Callable[[Any, booking.Clinic], Awaitable[dict]]) -> dict:
    pool = await db.get_pool()
    async with tenant_conn(pool, explicit_tenant=tenant_id, reason="clinic-tool-call") as conn:
        clinic = await booking.load_clinic(conn, tenant_id)
        result = await work(conn, clinic)
    # Sentences that toolexec speaks through a template also go out in template-sized parts.
    for key in ("confirm_text", "spoken"):
        if key in result:
            booking.with_parts(result, key)
    return result


@router.post("/departments")
async def departments(body: DepartmentsBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    language = speech.lang_or_default(body.language)

    async def work(conn, clinic):
        rows = await booking.list_departments(conn, clinic)
        return {"ok": True, "hospital": clinic.settings["hospital_name"], "departments": [
            {"code": r["code"], "name": speech.department_name(r, language), "about": r["description"],
             "location": r["location"], "treats": list(r["symptoms"]), "doctor_count": r["doctor_count"]}
            for r in rows
        ]}
    return await _run(tenant_id, work)


@router.post("/doctors")
async def doctors(body: DoctorsBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    language = speech.lang_or_default(body.language)

    async def work(conn, clinic):
        department_id = None
        if body.department:
            dept = await booking.find_department(conn, clinic, body.department)
            if dept is None:
                return {"ok": False, "message": f"No department matches {body.department!r}. Use list_departments."}
            department_id = dept["id"]
        found = await booking.find_doctors(
            conn, clinic, doctor=body.doctor, department_id=department_id, language=body.consult_language,
        )
        return {
            "ok": True, "doctors": [booking.doctor_view(d, language) for d in found[:8]],
            "message": "" if found else "No doctor matches. Try the department instead.",
        }
    return await _run(tenant_id, work)


@router.post("/availability")
async def availability(body: AvailabilityBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.availability(
        conn, clinic, doctor=body.doctor, department=body.department, date=body.date,
        part_of_day=body.part_of_day, language=body.language or "en", holder_phone=body.caller_phone,
    ))


@router.post("/bookings/prepare")
async def prepare_booking(body: PrepareBookingBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.prepare_booking(
        conn, clinic, doctor=body.doctor, slot_start=body.slot_start, patient_name=body.patient_name,
        patient_phone=body.patient_phone, caller_phone=body.caller_phone, language=body.language or "en",
    ))


@router.post("/bookings")
async def book(body: BookBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.book(
        conn, clinic, doctor_code=body.doctor_code, slot_start=body.slot_start, patient_name=body.patient_name,
        patient_phone=body.patient_phone, caller_phone=body.caller_phone, reason=body.reason,
        patient_age=body.patient_age, language=body.language or "en",
    ))


@router.post("/appointments/find")
async def find(body: FindBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.find_appointments(
        conn, clinic, caller_phone=body.caller_phone, reference=body.reference,
        patient_phone=body.patient_phone, language=body.language or "en",
    ))


@router.post("/reschedule/prepare")
async def prepare_reschedule(body: PrepareRescheduleBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.prepare_reschedule(
        conn, clinic, reference=body.reference, new_slot_start=body.new_slot_start,
        caller_phone=body.caller_phone, patient_phone=body.patient_phone, language=body.language or "en",
        doctor=body.doctor,
    ))


@router.post("/reschedule")
async def reschedule(body: PrepareRescheduleBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.reschedule(
        conn, clinic, reference=body.reference, new_slot_start=body.new_slot_start,
        caller_phone=body.caller_phone, patient_phone=body.patient_phone, language=body.language or "en",
        doctor_code=body.doctor_code,
    ))


@router.post("/cancel/prepare")
async def prepare_cancel(body: CancelBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.prepare_cancel(
        conn, clinic, reference=body.reference, caller_phone=body.caller_phone,
        patient_phone=body.patient_phone, language=body.language or "en",
    ))


@router.post("/cancel")
async def cancel(body: CancelBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.cancel(
        conn, clinic, reference=body.reference, caller_phone=body.caller_phone,
        patient_phone=body.patient_phone, reason=body.reason, language=body.language or "en",
    ))


@router.post("/reminder/context")
async def reminder_context(body: _Body, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.reminder_context(
        conn, clinic, caller_phone=body.caller_phone, language=body.language or "en",
    ))


@router.post("/reminder/outcome")
async def reminder_outcome(body: ReminderOutcomeBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, clinic: booking.record_reminder_outcome(
        conn, clinic, reference=body.reference, outcome=body.outcome, caller_phone=body.caller_phone,
        language=body.language or "en",
    ))
