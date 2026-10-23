"""calendar_sync: deterministic-id upsert/delete against Google, token kept inside toolexec."""

from __future__ import annotations

import httpx
import pytest

from services.toolexec import calendar_sync, oauth


class _Provider:
    def __init__(self, key="google"):
        self.key = key


@pytest.fixture
def google(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []
    responses: list[httpx.Response] = []

    async def _token(tenant_id, connection_id):
        return "tok", _Provider(), None, "oauth2"

    async def _call(method, url, *, data=None, json_body=None, headers=None):
        assert headers == {"Authorization": "Bearer tok"}
        calls.append((method, url, json_body))
        return responses.pop(0)

    monkeypatch.setattr(oauth, "access_token_for", _token)
    monkeypatch.setattr(oauth, "_provider_call", _call)
    return calls, responses


EVENT = {"summary": "Rahul - Dr. Mehta (#482913)", "start": {}, "end": {}}
BASE = "https://www.googleapis.com/calendar/v3/calendars/dr.mehta%40example.com/events"


async def _sync(event=EVENT):
    return await calendar_sync.sync_event(
        tenant_id="t", connection_id="c", calendar_id="dr.mehta@example.com", event_id="clinicabc123", event=event,
    )


@pytest.mark.asyncio
async def test_update_existing_event(google):
    calls, responses = google
    responses.append(httpx.Response(200, json={"htmlLink": "https://cal/x"}))
    assert (await _sync())["html_link"] == "https://cal/x"
    assert calls == [("PUT", f"{BASE}/clinicabc123", EVENT)]


@pytest.mark.asyncio
async def test_create_when_missing_uses_same_id(google):
    calls, responses = google
    responses += [httpx.Response(404), httpx.Response(200, json={})]
    await _sync()
    assert calls[1] == ("POST", BASE, {**EVENT, "id": "clinicabc123"})


@pytest.mark.asyncio
async def test_delete_is_idempotent(google):
    calls, responses = google
    responses.append(httpx.Response(410))
    assert (await _sync(event=None))["deleted"]
    assert calls == [("DELETE", f"{BASE}/clinicabc123", None)]


@pytest.mark.asyncio
async def test_permission_error_not_retryable_server_error_is(google):
    _, responses = google
    responses.append(httpx.Response(403))
    with pytest.raises(calendar_sync.CalendarSyncFailed) as exc:
        await _sync()
    assert exc.value.code == "google_status_403" and not exc.value.retryable
    responses.append(httpx.Response(503))
    with pytest.raises(calendar_sync.CalendarSyncFailed) as exc:
        await _sync()
    assert exc.value.retryable


@pytest.mark.asyncio
async def test_non_google_connection_refused(monkeypatch):
    async def _token(tenant_id, connection_id):
        return "tok", _Provider("hubspot"), None, "oauth2"

    monkeypatch.setattr(oauth, "access_token_for", _token)
    with pytest.raises(calendar_sync.CalendarSyncFailed, match="not_a_google_connection"):
        await _sync()
