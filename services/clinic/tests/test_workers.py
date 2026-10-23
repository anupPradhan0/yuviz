"""Outbox delivery (Twilio, calendar) and the reminder dialler, with fakes for the outside world."""

from __future__ import annotations

import httpx
import pytest

from services.clinic import booking, integrations, outbox, reminders

from .conftest import tomorrow_at
from .test_booking import _prepare_and_book


class FakeCalendar:
    def __init__(self, fail_with: integrations.DeliveryFailed | None = None):
        self.calls: list[dict] = []
        self.fail_with = fail_with

    async def sync(self, payload):
        self.calls.append(payload)
        if self.fail_with:
            raise self.fail_with
        return {"ok": True, "event_id": payload["event_id"]}

    async def close(self):
        pass


def _twilio(handler):
    return httpx.MockTransport(handler)


async def _drain(worker: outbox.OutboxWorker, pool, tenant_id: str) -> None:
    # Make everything due now, then deliver.
    await pool.execute("UPDATE clinic_outbox SET next_attempt_at = now() WHERE tenant_id = $1 AND status = 'pending'",
                       tenant_id)
    await worker.tick()


async def test_outbox_sends_sms_and_syncs_calendar(run, pool, clinic_tenant, monkeypatch):
    monkeypatch.setenv("CLINIC_TEST_TWILIO_TOKEN", "secret-token")
    sent = []

    def twilio(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2010-04-01/Accounts/ACtest/Messages.json"
        assert request.headers["authorization"].startswith("Basic ")
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(201, json={"sid": "SM123"})

    calendar = FakeCalendar()
    worker = outbox.OutboxWorker(calendar=calendar, sms_transport=_twilio(twilio))
    _, done = await _prepare_and_book(run, slot=tomorrow_at(9, 0), caller="+919822200001", name="Kiran")
    await _drain(worker, pool, clinic_tenant["id"])

    mine = [s for s in sent if s["To"] == "+919822200001"]
    assert len(mine) == 1 and done["reference"] in mine[0]["Body"] and mine[0]["From"] == "+15005550006"
    event = next(c for c in calendar.calls if c["event"] and done["reference"] in c["event"]["summary"])
    assert event["calendar_id"] == "primary" and event["event"]["start"]["timeZone"] == "Asia/Kolkata"
    status = await pool.fetch(
        "SELECT o.kind, o.status FROM clinic_outbox o JOIN clinic_appointments a ON a.id = o.appointment_id "
        "WHERE a.reference = $1", done["reference"],
    )
    assert {r["status"] for r in status} == {"done"}

    # Cancelling re-arms the same calendar row, which now deletes the event.
    await run(booking.cancel, reference=done["reference"], caller_phone="+919822200001", patient_phone=None,
              reason=None, language="en")
    calendar.calls.clear()
    await _drain(worker, pool, clinic_tenant["id"])
    assert any(c["event"] is None and c["event_id"] == event["event_id"] for c in calendar.calls)


async def test_outbox_retries_then_gives_up_on_permanent_error(run, pool, clinic_tenant, monkeypatch):
    monkeypatch.setenv("CLINIC_TEST_TWILIO_TOKEN", "secret-token")
    worker = outbox.OutboxWorker(
        calendar=FakeCalendar(fail_with=integrations.DeliveryFailed("calendar_503", retryable=True)),
        sms_transport=_twilio(lambda r: httpx.Response(400, json={"code": 21211})),
    )
    _, done = await _prepare_and_book(run, slot=tomorrow_at(9, 15), caller="+919822200002", name="Neha")
    await _drain(worker, pool, clinic_tenant["id"])
    rows = {r["kind"]: r for r in await pool.fetch(
        "SELECT o.kind, o.status, o.attempts, o.last_error FROM clinic_outbox o "
        "JOIN clinic_appointments a ON a.id = o.appointment_id WHERE a.reference = $1", done["reference"],
    )}
    assert rows["sms"]["status"] == "dead" and rows["sms"]["last_error"] == "twilio_400_21211"
    assert rows["gcal_sync"]["status"] == "pending" and rows["gcal_sync"]["attempts"] == 1


def test_backoff_is_capped():
    assert [outbox.backoff_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert outbox.backoff_seconds(20) == 3600


# ── reminders ──

def test_originate_command_shape(monkeypatch):
    monkeypatch.setenv("CLINIC_DIAL_FORMAT", "national0")
    monkeypatch.delenv("CLINIC_REMINDER_SIP_PROFILE", raising=False)
    command = reminders.originate_command(
        patient_e164="+919876543210", agent_did="+91 1205108341", caller_id="01205108339", gateway="10.0.151.12:5060",
    )
    assert command == (
        "bgapi originate {origination_caller_id_number=01205108339,originate_timeout=40,ignore_early_media=true,"
        "yuviz_remote_ani=919876543210}sofia/external/sip:09876543210@10.0.151.12:5060 911205108341 XML public"
    )


def test_originate_command_to_named_extension(monkeypatch):
    monkeypatch.setenv("CLINIC_REMINDER_SIP_PROFILE", "internal")
    command = reminders.originate_command(
        patient_e164="+919876543210", agent_did="clinic-reminders", caller_id="01205108340",
        gateway="10.0.151.12:5060",
    )
    assert command.endswith("sofia/internal/sip:09876543210@10.0.151.12:5060 clinic-reminders XML public")


def test_originate_command_refuses_injection():
    with pytest.raises(reminders.OriginateError):
        reminders.originate_command(patient_e164="+919876543210", agent_did="911205108341",
                                    caller_id="0120 bad", gateway="10.0.151.12:5060")
    with pytest.raises(reminders.OriginateError):
        reminders.originate_command(patient_e164="+919876543210", agent_did="911205108341",
                                    caller_id="01205108339", gateway="host:5060 &bridge(x)")
    with pytest.raises(reminders.OriginateError):
        reminders.originate_command(patient_e164="+919876543210", agent_did="x XML default &bridge",
                                    caller_id="01205108339", gateway="10.0.151.12:5060")


async def test_reminder_worker_dials_retries_then_falls_back_to_sms(run, pool, clinic_tenant, monkeypatch):
    monkeypatch.setenv("CLINIC_REMINDER_SIP_GATEWAY", "10.0.151.12:5060")
    commands = []

    async def fake_originate(command: str) -> str:
        commands.append(command)
        return f"job-{len(commands)}"

    worker = reminders.ReminderWorker(originate=fake_originate)
    caller = "+919822200003"
    _, done = await _prepare_and_book(run, slot=tomorrow_at(16, 45), caller=caller, name="Pooja")
    ref = done["reference"]

    async def due_now():
        await pool.execute("UPDATE clinic_appointments SET next_reminder_at = now() - interval '1 second' "
                           "WHERE reference = $1 AND next_reminder_at IS NOT NULL", ref)
        await worker.tick()

    await due_now()
    assert len(commands) == 1 and "yuviz_remote_ani=919822200003" in commands[0]
    row = await pool.fetchrow("SELECT reminder_status, reminder_attempts FROM clinic_appointments WHERE reference = $1", ref)
    assert (row["reminder_status"], row["reminder_attempts"]) == ("calling", 1)

    await worker.on_job_complete("job-1", False, "-ERR NO_ANSWER")
    await due_now()
    await worker.on_job_complete("job-2", False, "-ERR USER_BUSY")
    await due_now()
    await worker.on_job_complete("job-3", False, "-ERR NO_ANSWER")
    await due_now()   # 4th time: out of attempts -> SMS instead
    assert len(commands) == 3
    row = await pool.fetchrow("SELECT reminder_status, next_reminder_at FROM clinic_appointments WHERE reference = $1", ref)
    assert row["reminder_status"] == "unreachable" and row["next_reminder_at"] is None
    assert await pool.fetchval(
        "SELECT count(*) FROM clinic_outbox o JOIN clinic_appointments a ON a.id = o.appointment_id "
        "WHERE a.reference = $1 AND o.payload->>'event' = 'reminder'", ref) == 1


async def test_answered_reminder_stops_dialling(run, pool, monkeypatch):
    monkeypatch.setenv("CLINIC_REMINDER_SIP_GATEWAY", "10.0.151.12:5060")

    async def fake_originate(command: str) -> str:
        return "job-answered"

    worker = reminders.ReminderWorker(originate=fake_originate)
    _, done = await _prepare_and_book(run, slot=tomorrow_at(15, 45), caller="+919822200004", name="Sameer")
    await pool.execute("UPDATE clinic_appointments SET next_reminder_at = now() - interval '1 second' "
                       "WHERE reference = $1", done["reference"])
    await worker.tick()
    await worker.on_job_complete("job-answered", True, "+OK abc")
    row = await pool.fetchrow("SELECT reminder_status, next_reminder_at FROM clinic_appointments WHERE reference = $1",
                              done["reference"])
    assert row["reminder_status"] == "calling" and row["next_reminder_at"] is None
