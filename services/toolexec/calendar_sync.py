"""Writes one Google Calendar event on a tenant's Google connection, for a backend service
that keeps its own records (the clinic service's appointments) and mirrors them to Google.

The token never leaves this service: callers send the event, not a request for a token.
Event ids are chosen by the caller and deterministic, so a retried upsert updates the
same event instead of creating a second one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import oauth

_GOOGLE_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"
_GONE = (404, 410)


class CalendarSyncFailed(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


async def sync_event(
    *, tenant_id: str, connection_id: str, calendar_id: str, event_id: str, event: dict[str, Any] | None,
) -> dict[str, Any]:
    """Upsert `event` under `event_id`, or delete it when `event` is None."""
    try:
        token, provider, _api_base, _kind = await oauth.access_token_for(tenant_id, connection_id)
    except oauth.ReconnectRequired:
        raise CalendarSyncFailed("reconnect_required", retryable=False) from None
    if provider.key != "google":
        raise CalendarSyncFailed("not_a_google_connection", retryable=False)

    events_url = _GOOGLE_EVENTS.format(calendar=quote(calendar_id, safe=""))
    event_url = f"{events_url}/{event_id}"
    headers = {"Authorization": f"Bearer {token}"}

    if event is None:
        resp = await oauth._provider_call("DELETE", event_url, headers=headers)
        if resp.status_code in (200, 204) or resp.status_code in _GONE:
            return {"ok": True, "event_id": event_id, "deleted": True}
        raise _failure(resp.status_code)

    resp = await oauth._provider_call("PUT", event_url, json_body=event, headers=headers)
    if resp.status_code in _GONE:
        resp = await oauth._provider_call("POST", events_url, json_body={**event, "id": event_id}, headers=headers)
    if 200 <= resp.status_code < 300:
        return {"ok": True, "event_id": event_id, "html_link": resp.json().get("htmlLink")}
    raise _failure(resp.status_code)


def _failure(status: int) -> CalendarSyncFailed:
    # 401/403: the grant lacks calendar scope or the calendar isn't shared with it; retrying won't help.
    return CalendarSyncFailed(f"google_status_{status}", retryable=status == 429 or status >= 500)
