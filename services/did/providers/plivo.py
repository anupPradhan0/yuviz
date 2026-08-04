"""
PlivoProvider — IDidProvider backed by Plivo's REST API.

*** UNVERIFIED, 2026-07-23 — READ BEFORE TRUSTING THIS FILE ***
Every endpoint, field name, and response shape below is built from Plivo's
public API documentation as captured in training data, NOT confirmed
against a real account (no trial credentials existed yet when this was
written — see project memory did-management-platform-architecture). This
project's established discipline (Cal.com, Gemini) is to never trust a
vendor shape until it's been hit live — every prior "guessed from docs"
attempt in this codebase turned up at least one real surprise (Cloudflare
blocking a bare HTTP client, a required `thoughtSignature` field
undocumented in the obvious places, per-endpoint API-version headers that
looked like typos but weren't). Treat every field name and status code
here as a hypothesis, not a fact, until it's been checked against a real
sandbox call. When that happens, replace this docstring's warning with a
confirmation note (see cal_com.py's own docstring for the tone to match).

Assumed API shape:
  - Base URL: https://api.plivo.com/v1/Account/{auth_id}
  - Auth: HTTP Basic (auth_id, auth_token) — not a bearer token.
  - Search:  GET  /PhoneNumber/?country_iso=US&pattern=415&type=local
  - Buy:     POST /PhoneNumber/{number}/
  - Release: DELETE /Number/{number}/
  - Plivo has no separate per-number resource id the way Twilio has a PNxxxx
    SID — the E.164 number itself is the identifier used in the release
    URL, so carrier_number_sid is just phone_number for this provider
    (documented on PurchasedNumber, not assumed elsewhere in this codebase).
"""

from __future__ import annotations

import logging

import httpx

from .interface import AvailableNumber, DidProviderError, PurchasedNumber

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.plivo.com"
_TYPE_MAP = {"US": "local"}  # assumed default; unverified for non-US countries


class PlivoProvider:
    def __init__(
        self,
        auth_id:    str,
        auth_token: str,
        base_url:   str = _DEFAULT_BASE_URL,
        timeout_s:  float = 15.0,
    ) -> None:
        self._auth_id = auth_id
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/v1/Account/{auth_id}",
            auth=(auth_id, auth_token),
            timeout=timeout_s,
        )
        log.info("PlivoProvider auth_id=%s (UNVERIFIED provider — see module docstring)", auth_id)

    async def search_available_numbers(
        self, country: str, area_code: str | None = None, limit: int = 10,
    ) -> list[AvailableNumber]:
        params: dict[str, str | int] = {
            "country_iso": country,
            "type": _TYPE_MAP.get(country.upper(), "local"),
            "limit": limit,
        }
        if area_code:
            params["pattern"] = area_code

        try:
            resp = await self._client.get("/PhoneNumber/", params=params)
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo /PhoneNumber/ search returned {resp.status_code}: {resp.text}")

        data = resp.json()
        return [
            AvailableNumber(
                phone_number=_to_e164(obj["number"]),
                region=obj.get("region"),
                monthly_price=obj.get("monthly_rental_rate"),
                capabilities=_capabilities_from(obj),
            )
            for obj in data.get("objects", [])
        ]

    async def purchase_number(self, phone_number: str) -> PurchasedNumber:
        plivo_number = phone_number.lstrip("+")
        try:
            resp = await self._client.post(f"/PhoneNumber/{plivo_number}/", json={})
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo purchase of {phone_number} returned {resp.status_code}: {resp.text}")

        # Plivo has no separate resource id — the number itself is what
        # DELETE /Number/{number}/ expects later (see module docstring).
        return PurchasedNumber(phone_number=phone_number, carrier_number_sid=plivo_number)

    async def release_number(self, carrier_number_sid: str) -> None:
        plivo_number = carrier_number_sid.lstrip("+")
        try:
            resp = await self._client.delete(f"/Number/{plivo_number}/")
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo release of {carrier_number_sid} returned {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_e164(plivo_number: str) -> str:
    """Plivo's own number strings have no leading '+' (assumed, unverified)
    — this project's AvailableNumber.phone_number is always E.164."""
    return plivo_number if plivo_number.startswith("+") else f"+{plivo_number}"


def _capabilities_from(obj: dict) -> tuple[str, ...]:
    caps = []
    if obj.get("voice_enabled"):
        caps.append("voice")
    if obj.get("sms_enabled"):
        caps.append("sms")
    if obj.get("mms_enabled"):
        caps.append("mms")
    return tuple(caps)
