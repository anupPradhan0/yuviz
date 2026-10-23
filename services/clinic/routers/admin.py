"""Front-desk endpoints: appointments, doctors and leave, for console users of the tenant."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from libs.tenancy import tenant_conn
from services.config.auth import CurrentUser
from services.config.deps import bind_path_tenant, get_current_user, require_path_tenant_access, require_role

from .. import booking, db

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/clinic", tags=["clinic"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)

_STAFF = require_role("superadmin", "admin", "supervisor", "agent")


async def _scoped_tenant(conn) -> str:
    """The path segment may be a slug or a UUID; tenant_conn has already resolved it."""
    return await conn.fetchval("SELECT current_setting('app.tenant_id')")
_ADMIN = require_role("superadmin", "admin")


@tenant_scoped_router.get("/doctors")
async def list_doctors(tenant_id: str, current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        tenant_id = await _scoped_tenant(conn)
        clinic = await booking.load_clinic(conn, tenant_id)
        return [booking.doctor_view(d, "en") for d in await booking.find_doctors(conn, clinic)]


@tenant_scoped_router.get("/appointments")
async def list_appointments(
    tenant_id: str,
    day_from: datetime.date | None = Query(default=None, alias="from"),
    day_to: datetime.date | None = Query(default=None, alias="to"),
    doctor: str | None = None,
    current_user: CurrentUser = Depends(_STAFF),
):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        tenant_id = await _scoped_tenant(conn)
        clinic = await booking.load_clinic(conn, tenant_id)
        first = day_from or clinic.today
        last = day_to or first + datetime.timedelta(days=clinic.settings["booking_window_days"])
        rows = await conn.fetch(
            "SELECT a.reference, a.patient_name, a.patient_phone, a.patient_age, a.reason, a.language, a.status, "
            "       a.starts_at, a.ends_at, a.reminder_status, a.created_at, d.code AS doctor_code, "
            "       d.name AS doctor_name, dep.name AS department "
            "FROM clinic_appointments a JOIN clinic_doctors d ON d.id = a.doctor_id "
            "JOIN clinic_departments dep ON dep.id = d.department_id "
            "WHERE a.tenant_id = $1 AND a.starts_at >= $2 AND a.starts_at < $3 "
            "  AND ($4::text IS NULL OR lower(d.code) = lower($4)) "
            "ORDER BY a.starts_at",
            tenant_id,
            datetime.datetime.combine(first, datetime.time.min, tzinfo=clinic.tz),
            datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time.min, tzinfo=clinic.tz),
            doctor,
        )
        return [{**dict(r), "starts_at": clinic.local(r["starts_at"]).isoformat(),
                 "ends_at": clinic.local(r["ends_at"]).isoformat()} for r in rows]


class StatusUpdate(BaseModel):
    status: str


@tenant_scoped_router.patch("/appointments/{reference}")
async def update_appointment_status(
    tenant_id: str, reference: str, body: StatusUpdate, current_user: CurrentUser = Depends(_STAFF),
):
    """Front desk marks a visit completed or no-show after the time has passed."""
    if body.status not in ("completed", "no_show"):
        raise HTTPException(status_code=400, detail="status must be completed or no_show")
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        tenant_id = await _scoped_tenant(conn)
        row = await conn.fetchrow(
            "UPDATE clinic_appointments SET status = $3, next_reminder_at = NULL, updated_at = now() "
            "WHERE tenant_id = $1 AND reference = $2 AND status = 'booked' AND starts_at <= now() "
            "RETURNING reference, status",
            tenant_id, reference, body.status,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="appointment not found, not booked, or not yet due")
    return dict(row)


class LeaveCreate(BaseModel):
    doctor_code: str
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    reason: str = ""


@tenant_scoped_router.post("/leaves", status_code=201)
async def add_leave(tenant_id: str, body: LeaveCreate, current_user: CurrentUser = Depends(_ADMIN)):
    """Blocks the doctor's time. Bookings already inside it are returned for the front desk to
    move; they are not cancelled automatically, because a patient must hear about it first."""
    if body.starts_at.tzinfo is None or body.ends_at.tzinfo is None or body.starts_at >= body.ends_at:
        raise HTTPException(status_code=400, detail="starts_at and ends_at need offsets and starts_at < ends_at")
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        tenant_id = await _scoped_tenant(conn)
        clinic = await booking.load_clinic(conn, tenant_id)
        doctors = await booking.find_doctors(conn, clinic, doctor=body.doctor_code)
        if len(doctors) != 1:
            raise HTTPException(status_code=404, detail="doctor not found")
        doctor = doctors[0]
        await conn.execute(
            "INSERT INTO clinic_doctor_leaves (tenant_id, doctor_id, starts_at, ends_at, reason) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id, doctor["id"], body.starts_at, body.ends_at, body.reason,
        )
        clashes = await conn.fetch(
            "SELECT reference, patient_name, patient_phone, starts_at FROM clinic_appointments "
            "WHERE doctor_id = $1 AND status = 'booked' AND starts_at < $3 AND ends_at > $2 ORDER BY starts_at",
            doctor["id"], body.starts_at, body.ends_at,
        )
    return {"ok": True, "affected_appointments": [
        {**dict(r), "starts_at": clinic.local(r["starts_at"]).isoformat()} for r in clashes
    ]}

