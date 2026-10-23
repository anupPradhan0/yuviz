"""DB fixtures. Point CLINIC_TEST_DSN at a database that has schema.sql + clinic_schema.sql
applied (default: POSTGRES_DSN, else the local scratch database `clinic_scratch`)."""

from __future__ import annotations

import datetime
import os
import uuid

# Conftests load for the whole run: keep the suite's POSTGRES_DSN unless a clinic DB is named.
os.environ["POSTGRES_DSN"] = (
    os.environ.get("CLINIC_TEST_DSN") or os.environ.get("POSTGRES_DSN")
    or "postgresql:///clinic_scratch?host=/var/run/postgresql"
)
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

import pytest_asyncio  # noqa: E402

from libs.tenancy import tenant_conn  # noqa: E402
from services.clinic import booking, db  # noqa: E402

CLINIC_TABLES = (
    "clinic_reminder_calls", "clinic_outbox", "clinic_slot_holds", "clinic_appointments", "clinic_doctor_leaves",
    "clinic_doctor_schedules", "clinic_doctors", "clinic_departments", "clinic_api_keys", "clinic_settings",
)


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    yield await db.get_pool()


@pytest_asyncio.fixture(loop_scope="session")
async def clinic_tenant(pool):
    """A tenant with one department, two doctors seeing patients every day 09:00-17:00 in
    15-minute slots, SMS and calendar sync on (so outbox rows are written)."""
    slug = f"clinic-test-{uuid.uuid4().hex[:8]}"
    tenant_id = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug)
    await pool.execute(
        "INSERT INTO clinic_settings (tenant_id, hospital_name, hospital_phone, sms_enabled, twilio_account_sid, "
        "  twilio_auth_token_ref, twilio_from, google_connection_id, reminder_enabled, reminder_agent_did, "
        "  reminder_caller_id) "
        "VALUES ($1, 'Test Hospital', '01200000000', true, 'ACtest', 'env:CLINIC_TEST_TWILIO_TOKEN', '+15005550006', "
        "  gen_random_uuid(), true, '911205108341', '01205108339')",
        tenant_id,
    )
    dept = await pool.fetchval(
        "INSERT INTO clinic_departments (tenant_id, code, name, name_hi, location) "
        "VALUES ($1, 'CARD', 'Cardiology', 'हृदय रोग विभाग', 'Block A, 2nd floor') RETURNING id", tenant_id,
    )
    doctors = {}
    for code, name, fee in (("CARD-01", "Dr. Anjali Mehta", 900), ("CARD-02", "Dr. Vikram Rao", 800)):
        doctors[code] = await pool.fetchval(
            "INSERT INTO clinic_doctors (tenant_id, department_id, code, name, consultation_fee, slot_minutes) "
            "VALUES ($1, $2, $3, $4, $5, 15) RETURNING id", tenant_id, dept, code, name, fee,
        )
        for weekday in range(7):
            await pool.execute(
                "INSERT INTO clinic_doctor_schedules (tenant_id, doctor_id, weekday, start_time, end_time) "
                "VALUES ($1, $2, $3, '09:00', '17:00')", tenant_id, doctors[code], weekday,
            )
    yield {"id": str(tenant_id), "doctors": doctors, "department": dept}
    for table in CLINIC_TABLES:
        await pool.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant_id)


@pytest_asyncio.fixture(loop_scope="session")
async def run(pool, clinic_tenant):
    """run(fn, **kwargs): calls booking.fn(conn, clinic, **kwargs) in one tenant transaction."""
    async def _run(fn, **kwargs):
        async with tenant_conn(pool, explicit_tenant=clinic_tenant["id"], reason="test") as conn:
            clinic = await booking.load_clinic(conn, clinic_tenant["id"])
            return await fn(conn, clinic, **kwargs)
    return _run


def tomorrow_at(hour: int, minute: int = 0) -> str:
    """A slot start in the clinic's timezone, tomorrow — always inside the window and lead time."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Kolkata")
    day = datetime.datetime.now(tz).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).isoformat()
