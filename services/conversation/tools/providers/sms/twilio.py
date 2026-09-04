"""
TwilioSmsProvider — ISmsProvider backed by Twilio's Messages REST API.
Same HTTP Basic auth (AccountSid/AuthToken) as services/did's TwilioProvider,
different resource. Live-verified: POST /2010-04-01/Accounts/{Sid}/Messages.json,
form-encoded To/From/Body, 201 on success.

Gotcha: a trial account can only text numbers verified in the console —
sending to an unverified one 400s with error code 21608.
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
