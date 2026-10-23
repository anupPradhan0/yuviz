"""Outbound clients: Twilio SMS, and Google Calendar through toolexec."""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any

import httpx

from libs.config_sdk.secret_resolver import EnvResolver
from libs.config_sdk.secrets import decrypt_tenant_secret, is_tenant_bound

log = logging.getLogger(__name__)

_TWILIO_API = "https://api.twilio.com"
_TIMEOUT_S = 10.0


class DeliveryFailed(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


async def resolve_secret(tenant_id: str, ref: str | None) -> str:
    """'enc:t1.' (tenant-bound) or 'env:NAME' (through the shared EnvResolver). Anything else is refused."""
    if not ref:
        raise DeliveryFailed("secret_missing", retryable=False)
    if ref.startswith("enc:"):
        if not is_tenant_bound(ref):
            raise DeliveryFailed("secret_not_tenant_bound", retryable=False)
        return decrypt_tenant_secret(tenant_id, ref)
    if ref.startswith("env:"):
        try:
            value = await EnvResolver().resolve(ref)
        except KeyError:
            value = ""
        if not value:
            raise DeliveryFailed("secret_env_unset", retryable=False)
        return value
    raise DeliveryFailed("secret_ref_unsupported", retryable=False)


async def send_twilio_sms(*, tenant_id: str, settings: dict, to: str, body: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> str:
    sid = settings.get("twilio_account_sid")
    sender = settings.get("twilio_from")
    if not sid or not sender:
        raise DeliveryFailed("twilio_not_configured", retryable=False)
    token = await resolve_secret(tenant_id, settings.get("twilio_auth_token_ref"))
    url = f"{_TWILIO_API}/2010-04-01/Accounts/{sid}/Messages.json"
    # A messaging-service SID ('MG…') goes in its own field; anything else is a sender number or ID.
    sender_field = "MessagingServiceSid" if sender.startswith("MG") else "From"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
            resp = await client.post(url, auth=(sid, token), data={"To": to, sender_field: sender, "Body": body})
    except httpx.HTTPError as exc:
        raise DeliveryFailed(f"twilio_unreachable:{type(exc).__name__}", retryable=True) from None
    if resp.status_code in (200, 201):
        return resp.json().get("sid", "")
    try:
        code = resp.json().get("code")
    except ValueError:
        code = None
    # 4xx is a bad number, unverified trial recipient or blocked sender: retrying sends nothing new.
    raise DeliveryFailed(f"twilio_{resp.status_code}_{code}", retryable=resp.status_code == 429 or resp.status_code >= 500)


class ToolexecCalendar:
    """Logs in to Config as the clinic's service account and calls toolexec's calendar endpoint."""

    def __init__(self, *, base_url: str, auth_base_url: str, email: str, password: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=_TIMEOUT_S * 2, transport=transport)
        self._auth = httpx.AsyncClient(base_url=auth_base_url.rstrip("/"), timeout=_TIMEOUT_S, transport=transport)
        self._email = email
        self._password = password
        self._token: str | None = None

    @classmethod
    def from_env(cls) -> "ToolexecCalendar | None":
        base = os.environ.get("TOOLEXEC_SERVICE_URL")
        auth = os.environ.get("CONFIG_SERVICE_URL")
        email = os.environ.get("CLINIC_SERVICE_EMAIL")
        password = os.environ.get("CLINIC_SERVICE_PASSWORD")
        if not (base and auth and email and password):
            return None
        return cls(base_url=base, auth_base_url=auth, email=email, password=password)

    async def close(self) -> None:
        await self._client.aclose()
        await self._auth.aclose()

    async def _login(self) -> str:
        resp = await self._auth.post("/auth/login", json={"email": self._email, "password": self._password})
        if resp.status_code != 200:
            raise DeliveryFailed(f"service_login_{resp.status_code}", retryable=True)
        return resp.json()["access_token"]

    async def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._token is None:
                self._token = await self._login()
            resp = await self._client.post("/internal/calendar/events/sync", json=payload,
                                            headers={"Authorization": f"Bearer {self._token}"})
            if resp.status_code == 401:
                self._token = await self._login()
                resp = await self._client.post("/internal/calendar/events/sync", json=payload,
                                                headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise DeliveryFailed(f"toolexec_unreachable:{type(exc).__name__}", retryable=True) from None
        if resp.status_code == 200:
            return resp.json()
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        raise DeliveryFailed(f"calendar_{resp.status_code}_{detail}", retryable=resp.status_code >= 500)


def calendar_event_id(appointment_id: Any) -> str:
    """Deterministic, and in Google's base32hex alphabet (hex digits are a subset)."""
    return "clinic" + str(appointment_id).replace("-", "")


def calendar_event(*, settings: dict, appointment: dict, doctor: dict, department: dict,
                   tz_name: str) -> dict[str, Any]:
    def _at(moment: datetime.datetime) -> dict:
        return {"dateTime": moment.isoformat(), "timeZone": tz_name}

    lines = [
        f"Patient: {appointment['patient_name']}"
        + (f", {appointment['patient_age']} yrs" if appointment.get("patient_age") is not None else ""),
        f"Phone: {appointment['patient_phone']}",
        f"Booking no.: {appointment['reference']}",
        f"Department: {department['name']}",
    ]
    if appointment.get("reason"):
        lines.append(f"Reason: {appointment['reason']}")
    lines.append(f"Booked via {settings['hospital_name']} voice assistant ({appointment['channel']}).")
    return {
        "summary": f"{appointment['patient_name']} - {doctor['name']} (#{appointment['reference']})",
        "description": "\n".join(lines),
        "location": doctor.get("department_location") or settings["hospital_name"],
        "start": _at(appointment["starts_at"]),
        "end": _at(appointment["ends_at"]),
        "extendedProperties": {"private": {"clinic_reference": appointment["reference"]}},
        "reminders": {"useDefault": True},
    }
