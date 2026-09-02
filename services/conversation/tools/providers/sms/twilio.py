"""
TwilioSmsProvider — ISmsProvider backed by Twilio's Programmable Messaging
REST API. Same auth/HTTP shape as services/did/providers/twilio.py's
TwilioProvider (HTTP Basic auth with AccountSid/AuthToken, same base URL) —
a distinct class because it's a different resource (Messages, not
IncomingPhoneNumbers/AvailablePhoneNumbers) serving a different layer of
the system (a live call's booking confirmation, not DID provisioning), not
because the underlying account/auth mechanics differ at all.

Confirmed API shape (Twilio's Messages resource, stable and documented for
over a decade — same "strong prior, not yet independently re-verified
against a live account" posture as purchase_number()/release_node() in the
DID Twilio provider, until a real trial-account send confirms it):
  POST /2010-04-01/Accounts/{AccountSid}/Messages.json
  form-encoded: To=+E164, From=+E164, Body=<text>
  Response: 201 Created with a "sid" (message resource id) on success.
A Twilio trial account can only send to a phone number that has been
verified in the console first — not a code limitation, a Twilio account
restriction; sending to an unverified number 400s with error code 21608.
"""

from __future__ import annotations

import logging

import httpx

from .interface import SmsProviderError

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.twilio.com"


class TwilioSmsProvider:
    def __init__(
        self,
        account_sid:  str,
        auth_token:   str,
        from_number:  str,
        base_url:     str = _DEFAULT_BASE_URL,
        timeout_s:    float = 10.0,
    ) -> None:
        self._from_number = from_number
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}",
            auth=(account_sid, auth_token),
            timeout=timeout_s,
        )
        log.info("TwilioSmsProvider account_sid=%s from=%s", account_sid, from_number)

    async def send_sms(self, to_number: str, body: str) -> None:
        try:
            resp = await self._client.post(
                "/Messages.json",
                data={"To": to_number, "From": self._from_number, "Body": body},
            )
        except httpx.HTTPError as exc:
            raise SmsProviderError(f"Twilio unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise SmsProviderError(f"Twilio SMS send to {to_number} returned {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        await self._client.aclose()
