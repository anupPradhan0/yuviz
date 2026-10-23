"""Delivers clinic_outbox rows: Google Calendar syncs and Twilio SMS, with retries."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import booking, db, integrations

log = logging.getLogger(__name__)

_POLL_S = 3.0
_MAX_ATTEMPTS = 8
_LEASE_SECONDS = 120


def backoff_seconds(attempts: int) -> int:
    return min(30 * 2 ** max(attempts - 1, 0), 3600)


class OutboxWorker:
    def __init__(self, *, calendar: integrations.ToolexecCalendar | None = None, sms_transport=None) -> None:
        self._calendar = calendar
        self._sms_transport = sms_transport
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._calendar is None:
            self._calendar = integrations.ToolexecCalendar.from_env()
            if self._calendar is None:
                log.warning("clinic outbox: CLINIC_SERVICE_EMAIL/PASSWORD or service URLs unset; "
                            "calendar sync rows will wait")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._calendar is not None:
            await self._calendar.close()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("clinic outbox: tick failed")
            await asyncio.sleep(_POLL_S)

    async def tick(self) -> int:
        pool = await db.get_pool()
        # Cross-tenant by design; each row is then handled under its own tenant's scope.
        async with platform_conn(pool, reason="clinic-outbox-claim") as conn:
            rows = await conn.fetch(
                "UPDATE clinic_outbox SET next_attempt_at = now() + make_interval(secs => $1) "
                "WHERE id IN (SELECT id FROM clinic_outbox WHERE status = 'pending' AND next_attempt_at <= now() "
                "             ORDER BY created_at LIMIT 20 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, tenant_id, appointment_id, kind, payload, rev, attempts",
                _LEASE_SECONDS,
            )
        for row in sorted(rows, key=lambda r: r["id"]):
            await self._deliver(dict(row))
        return len(rows)

    async def _deliver(self, row: dict) -> None:
        tenant_id = str(row["tenant_id"])
        pool = await db.get_pool()
        try:
            async with tenant_conn(pool, explicit_tenant=tenant_id, reason="clinic-outbox-deliver") as conn:
                clinic = await booking.load_clinic(conn, tenant_id)
                if row["kind"] == "sms":
                    await self._send_sms(clinic, row)
                else:
                    await self._sync_calendar(conn, clinic, row)
        except integrations.DeliveryFailed as exc:
            await self._failed(row, exc.code, retryable=exc.retryable)
            return
        except Exception as exc:  # a bug or a DB blip: retry with backoff, never lose the row
            log.exception("clinic outbox: delivery crashed id=%s", row["id"])
            await self._failed(row, f"crash:{type(exc).__name__}", retryable=True)
            return
        async with tenant_conn(pool, explicit_tenant=tenant_id, reason="clinic-outbox-done") as conn:
            # rev guard: a change that re-armed this row mid-delivery keeps it pending.
            await conn.execute(
                "UPDATE clinic_outbox SET status = 'done', done_at = now(), last_error = NULL "
                "WHERE id = $1 AND rev = $2", row["id"], row["rev"],
            )

    async def _failed(self, row: dict, code: str, *, retryable: bool) -> None:
        attempts = row["attempts"] + 1
        dead = not retryable or attempts >= _MAX_ATTEMPTS
        log.warning("clinic outbox: %s id=%s kind=%s attempt=%d error=%s",
                    "giving up" if dead else "retrying", row["id"], row["kind"], attempts, code)
        pool = await db.get_pool()
        async with tenant_conn(pool, explicit_tenant=str(row["tenant_id"]), reason="clinic-outbox-failed") as conn:
            await conn.execute(
                "UPDATE clinic_outbox SET attempts = $3, last_error = $4, "
                "  status = CASE WHEN $5 THEN 'dead' ELSE status END, "
                "  next_attempt_at = now() + make_interval(secs => $6) "
                "WHERE id = $1 AND rev = $2",
                row["id"], row["rev"], attempts, code[:300], dead, backoff_seconds(attempts),
            )

    async def _send_sms(self, clinic: booking.Clinic, row: dict) -> None:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        sid = await integrations.send_twilio_sms(
            tenant_id=clinic.tenant_id, settings=clinic.settings, to=payload["to"], body=payload["body"],
            transport=self._sms_transport,
        )
        log.info("clinic outbox: sms sent id=%s event=%s twilio_sid=%s", row["id"], payload.get("event"), sid)

    async def _sync_calendar(self, conn: Any, clinic: booking.Clinic, row: dict) -> None:
        if self._calendar is None:
            raise integrations.DeliveryFailed("calendar_client_unconfigured", retryable=True)
        connection_id = clinic.settings.get("google_connection_id")
        if connection_id is None:
            return  # sync was switched off after this row was queued
        appointment = dict(await conn.fetchrow("SELECT * FROM clinic_appointments WHERE id = $1", row["appointment_id"]))
        doctor = await conn.fetchrow(
            "SELECT d.*, dep.name AS department_name, dep.location AS department_location "
            "FROM clinic_doctors d JOIN clinic_departments dep ON dep.id = d.department_id WHERE d.id = $1",
            appointment["doctor_id"],
        )
        event_id = integrations.calendar_event_id(appointment["id"])
        event = None
        if appointment["status"] in ("booked", "completed", "no_show"):
            event = integrations.calendar_event(
                settings=clinic.settings,
                appointment={**appointment,
                             "starts_at": clinic.local(appointment["starts_at"]),
                             "ends_at": clinic.local(appointment["ends_at"])},
                doctor=dict(doctor), department={"name": doctor["department_name"]},
                tz_name=clinic.settings["timezone"],
            )
        await self._calendar.sync({
            "tenant_id": clinic.tenant_id, "connection_id": str(connection_id),
            "calendar_id": doctor["google_calendar_id"] or "primary",
            "event_id": event_id, "event": event,
        })
        await conn.execute(
            "UPDATE clinic_appointments SET gcal_event_id = $2 WHERE id = $1 AND gcal_event_id IS DISTINCT FROM $2",
            appointment["id"], event_id if event is not None else None,
        )
