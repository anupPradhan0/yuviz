"""Vobiz number ownership and inbound sync against a mocked Vobiz API
(endpoints per vobiz.ai/docs/applications/*)."""

from __future__ import annotations

import json

import httpx
import pytest

from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk.interface import InboundUrls
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider

ACCOUNT = "/api/v1/Account/MA_TEST"
URLS = InboundUrls(answer_url="https://p.test/vobiz/voice/cfg", hangup_url="https://p.test/vobiz/status/cfg")


def _provider(handler, **extra):
    p = VobizTelephonyProvider({"auth_id": "MA_TEST", "auth_token": "tok", **extra})
    p._http_transport = httpx.MockTransport(handler)
    return p


class _Vobiz:
    """Minimal stateful Vobiz: one number, applications, attachments."""

    def __init__(self, numbers=("+918065354620",), existing_apps=()):
        self.numbers = set(numbers)
        self.apps = {a: {} for a in existing_apps}
        self.attached = {}
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == f"{ACCOUNT}/numbers":
            items = [{"e164": n} for n in self.numbers if request.url.params.get("search") in (None, n)]
            return httpx.Response(200, json={"items": items, "total": len(items), "page": 1, "per_page": 100})
        if request.method == "POST" and path == f"{ACCOUNT}/Application/":
            app_id = f"app{len(self.apps) + 1}"
            self.apps[app_id] = body
            return httpx.Response(201, json={"app_id": app_id, "message": "created"})
        if request.method == "POST" and path.startswith(f"{ACCOUNT}/Application/"):
            app_id = path.rstrip("/").rsplit("/", 1)[1]
            if app_id not in self.apps:
                return httpx.Response(404, json={})
            self.apps[app_id].update(body)
            return httpx.Response(200, json={"message": "changed"})
        if path.endswith("/application") and "/numbers/" in path:
            number = request.url.path.split("/numbers/")[1].split("/")[0]
            if number not in self.numbers:
                return httpx.Response(404, json={})
            if request.method == "DELETE":
                self.attached.pop(number, None)
                return httpx.Response(200, json={})
            if body["application_id"] not in self.apps:
                return httpx.Response(404, json={})
            self.attached[number] = body["application_id"]
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"unexpected": path})


async def test_owns_number_matches_e164_with_or_without_plus():
    vobiz = _Vobiz()
    assert await _provider(vobiz).owns_number("+918065354620") is True
    assert await _provider(vobiz).owns_number("918065354620") is True
    assert await _provider(vobiz).owns_number("+911111111111") is False


async def test_owns_number_raises_when_vobiz_errors():
    with pytest.raises(TelephonyProviderError):
        await _provider(lambda r: httpx.Response(401, json={})).owns_number("+918065354620")


async def test_first_attach_creates_the_application_and_returns_its_id():
    vobiz = _Vobiz()
    result = await _provider(vobiz).attach_inbound("+918065354620", URLS, label="Yuviz - Vobiz")

    assert result.ok and result.credentials_update == {"inbound_application_id": "app1"}
    assert vobiz.apps["app1"]["answer_url"] == URLS.answer_url
    assert vobiz.apps["app1"]["hangup_url"] == URLS.hangup_url
    assert vobiz.attached == {"+918065354620": "app1"}


async def test_attach_reuses_the_stored_application_and_refreshes_its_urls():
    vobiz = _Vobiz(existing_apps=("app7",))
    result = await _provider(vobiz, inbound_application_id="app7").attach_inbound("+918065354620", URLS, label="x")

    assert result.ok and result.credentials_update is None
    assert vobiz.attached == {"+918065354620": "app7"}
    assert vobiz.apps["app7"]["answer_url"] == URLS.answer_url
    assert ("POST", f"{ACCOUNT}/Application/") not in vobiz.calls


async def test_a_deleted_stored_application_is_recreated_once():
    vobiz = _Vobiz()
    result = await _provider(vobiz, inbound_application_id="gone").attach_inbound("+918065354620", URLS, label="x")
    assert result.ok and result.credentials_update == {"inbound_application_id": "app1"}
    assert vobiz.attached == {"+918065354620": "app1"}


async def test_attach_of_a_number_outside_the_account_fails_clearly():
    vobiz = _Vobiz(numbers=())
    result = await _provider(vobiz).attach_inbound("+918065354620", URLS, label="x")
    assert not result.ok and "can't find" in result.message


async def test_detach_succeeds_and_treats_a_missing_number_as_done():
    vobiz = _Vobiz()
    vobiz.attached["+918065354620"] = "app1"
    assert (await _provider(vobiz).detach_inbound("+918065354620")).ok
    assert vobiz.attached == {}
    assert (await _provider(_Vobiz(numbers=())).detach_inbound("+918065354620")).ok


async def test_unreachable_vobiz_reports_instead_of_raising():
    def boom(request):
        raise httpx.ConnectError("down")

    result = await _provider(boom).attach_inbound("+918065354620", URLS, label="x")
    assert not result.ok and "couldn't reach Vobiz" in result.message
