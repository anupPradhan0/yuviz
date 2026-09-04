"""
TwilioSmsProvider tests — httpx.MockTransport, no real network.

*** These test the ASSUMED shape documented in twilio.py's module
docstring, not a confirmed real API contract *** — Twilio's Messages
resource has been stable/documented for over a decade, but this provider
has not yet been live-verified against a real (even trial) account. Same
posture as services/did/tests/test_twilio_provider.py's own disclaimer.
"""

from __future__ import annotations

import httpx
import pytest

from services.conversation.tools.providers.sms.interface import SmsProviderError
from services.conversation.tools.providers.sms.twilio import TwilioSmsProvider


def _make_provider(handler) -> TwilioSmsProvider:
    provider = TwilioSmsProvider(
        account_sid="ACtest", auth_token="test-token", from_number="+15005550006",
    )
    provider._client = httpx.AsyncClient(
        base_url="https://api.twilio.com/2010-04-01/Accounts/ACtest", transport=httpx.MockTransport(handler),
    )
    return provider


async def test_send_sms_posts_expected_form_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = dict(x.split("=") for x in request.content.decode().split("&"))
        return httpx.Response(201, json={"sid": "SMtest"})

    provider = _make_provider(handler)
    await provider.send_sms("+14155551234", "Your appointment is confirmed")

    assert seen["path"].endswith("/Messages.json")
    assert seen["body"]["To"] == "%2B14155551234"
    assert seen["body"]["From"] == "%2B15005550006"


async def test_send_sms_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21608, "message": "unverified number"})

    provider = _make_provider(handler)
    with pytest.raises(SmsProviderError):
        await provider.send_sms("+14155551234", "Hi")


async def test_send_sms_raises_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = _make_provider(handler)
    with pytest.raises(SmsProviderError):
        await provider.send_sms("+14155551234", "Hi")
