"""
Executor behaviour that connector presets rely on: nested bodies, path values
in the hashed arguments, the claim id as an idempotency field, response
transforms, the caller_id param source, the confirmation gate, the send cap and
claim release. Real Postgres; every outbound call goes to an httpx.MockTransport.

Rows are built from the real preset definitions (credentials swapped for
auth_scheme "none" so no OAuth connection is needed) or by hand.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio

from libs.tenancy import set_target_tenant, tenant_conn
from services.config.auth import CurrentUser
from services.toolexec import agent_apis, custom_apis, db, executor, presets
from services.toolexec.schemas import CalendarBookingSetup, WhatsAppSetup

from .test_chain_execution import _mock, _request


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


class Upstream:
    """Records every request and answers from `responses` by method and path."""

    def __init__(self, responses: dict[tuple[str, str], Any] | None = None, default: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}
        self.default = default if default is not None else (200, {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.get((request.method, request.url.path), self.default)
        return httpx.Response(status, json=body)

    def bodies(self) -> list[Any]:
        return [json.loads(r.content) for r in self.requests if r.content]


async def _insert(pool, tenant, agent, name: str, **overrides) -> dict:
    """A hand-built row, enabled for the agent."""
    kwargs = dict(
        tenant_id=tenant["id"], name=name, description="d", endpoint_url=f"https://{name}.example.com/api",
        method="POST", body_style="json", auth_scheme="none", auth_config={}, side_effecting=True,
        idempotency_header=None, timeout_ms=None, sensitive_response_paths=[], success_template=None, params=[],
    )
    kwargs.update(overrides)
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            row = await custom_apis._insert_custom_api(conn, **kwargs)
            await custom_apis._recompute_tenant_chain_levels(conn, tenant["id"])
    await agent_apis.set_enabled(
        agent["id"], row["id"], enabled=True,
        current_user=CurrentUser(id=str(uuid.uuid4()), email="a@t.example", role="admin", tenant_id=str(tenant["id"])),
    )
    return row


async def _apply(pool, tenant, agent, preset_key: str, steps: list[presets.PresetStep]) -> dict[str, dict]:
    """The real preset rows, with the credential swapped for none so no OAuth
    connection or stored key is needed."""
    steps = [dataclasses.replace(s, auth_scheme="none", auth_config={}) for s in steps]
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            rows = await presets._insert_missing_steps(
                conn, str(tenant["id"]), preset_key, steps, oauth_connection_id=None, secret_ref=None,
            )
            await custom_apis._recompute_tenant_chain_levels(conn, tenant["id"])
    user = CurrentUser(id=str(uuid.uuid4()), email="a@t.example", role="admin", tenant_id=str(tenant["id"]))
    for row in rows:
        await agent_apis.set_enabled(agent["id"], row["id"], enabled=True, current_user=user)
    return {r["name"]: r for r in rows}


async def _apply_calendar(pool, tenant, agent, **setup) -> dict[str, dict]:
    setup = CalendarBookingSetup(preset_key="calendar_booking", **setup)
    return await _apply(pool, tenant, agent, "calendar_booking", presets.PRESETS["calendar_booking"].steps(setup))


async def _apply_whatsapp(pool, tenant, agent, provider: str) -> dict[str, dict]:
    extra = {"gupshup": {"source_number": "919800000000", "app_name": "clinic"},
             "interakt": {}, "meta": {"phone_number_id": "123456789"}}[provider]
    setup = WhatsAppSetup(preset_key="whatsapp_confirmation", provider=provider, api_key="k", template="tpl", **extra)
    return await _apply(pool, tenant, agent, "whatsapp_confirmation", presets.PRESETS["whatsapp_confirmation"].steps(setup))


def _param(name: str, location: str = "body", source: str = "caller", **extra) -> dict:
    return {"name": name, "location": location, "json_type": "string", "source": source, **extra}


async def _steps(pool, tenant, api_name: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT s.* FROM api_chain_steps s JOIN api_chain_runs r ON r.id = s.run_id "
        "WHERE r.tenant_id = $1 AND s.api_name = $2 ORDER BY s.created_at", tenant["id"], api_name,
    )
    return [dict(r) for r in rows]


async def _hash_key() -> tuple[bytes, str]:
    return await executor._get_hmac_key()


# ── T20: argument plumbing and idempotency ───────────────────────────────

@pytest.mark.asyncio
async def test_flat_body_api_sends_the_same_body_and_hashes_as_before(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _insert(pool, tenant, agent, "flat", params=[
        _param("name"), _param("kind", "query"), _param("x-trace", "header"),
    ])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "flat", caller_arguments={"name": "Asha", "kind": "k", "x-trace": "t1"}))

    assert response.chain_status == "success"
    assert upstream.requests[0].content == b'{"name":"Asha"}'  # httpx's own compact encoding, untouched
    key, kid = await _hash_key()
    expected = executor._derive(
        "claim", str(tenant["id"]), str(api["id"]), {"name": "Asha", "kind": "k", "x-trace": "t1"}, key, kid)
    assert (await _steps(pool, tenant, "flat"))[0]["arguments_hash"] == expected


@pytest.mark.asyncio
async def test_body_path_builds_nested_dicts_and_lists_and_pads_skipped_slots(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "nested", params=[
        _param("who", body_path="person.name"),
        _param("when", body_path="slot.start.dateTime"),
        _param("a", body_path="values.0.0"),
        _param("c", body_path="values.0.2"),          # slot 1 left out: padded
        _param("mode", source="literal", literal_value="RAW", body_path="person.mode"),
    ])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    await executor.execute_chain(_request(
        tenant, agent, "nested", caller_arguments={"who": "Asha", "when": "T1", "a": "A", "c": "C"}))

    assert upstream.bodies() == [{
        "person": {"name": "Asha", "mode": "RAW"}, "slot": {"start": {"dateTime": "T1"}},
        "values": [["A", None, "C"]],
    }]


@pytest.mark.asyncio
async def test_form_body_json_encodes_a_nested_value_and_leaves_scalars(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "formy", body_style="form", params=[
        _param("channel", source="literal", literal_value="whatsapp"),
        _param("template_id", body_path="template.id"),
        _param("body_1", body_path="template.params.0"),
    ])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    await executor.execute_chain(_request(tenant, agent, "formy", caller_arguments={"template_id": "t", "body_1": "v"}))

    from urllib.parse import parse_qs
    form = parse_qs(upstream.requests[0].content.decode())
    assert form == {"channel": ["whatsapp"], "template": ['{"id": "t", "params": ["v"]}']}


@pytest.mark.asyncio
async def test_idempotency_body_field_is_the_claim_row_id_and_not_part_of_the_hash(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _insert(pool, tenant, agent, "idem", idempotency_body_field="id", params=[_param("title")])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    await executor.execute_chain(_request(tenant, agent, "idem", caller_arguments={"title": "T"}))

    claim = await pool.fetchrow("SELECT id, arguments_hash FROM api_side_effect_claims WHERE custom_api_id = $1", api["id"])
    assert upstream.bodies() == [{"title": "T", "id": claim["id"].hex}]
    key, kid = await _hash_key()
    assert claim["arguments_hash"] == executor._derive("claim", str(tenant["id"]), str(api["id"]), {"title": "T"}, key, kid)


@pytest.mark.asyncio
async def test_a_sensitive_path_value_is_redacted_yet_changes_the_hash(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "cancel", method="DELETE", endpoint_url="https://cancel.example.com/e/{event_id}",
                  params=[_param("event_id", "path", sensitive=True)])
    other = await _insert(pool, tenant, agent, "cancel_open", method="DELETE",
                          endpoint_url="https://cancel.example.com/e/{event_id}", params=[_param("event_id", "path")])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    for event in ("e1", "e2"):
        r = await executor.execute_chain(_request(tenant, agent, "cancel", caller_arguments={"event_id": event}))
        assert r.chain_status == "success"
    await executor.execute_chain(_request(tenant, agent, "cancel_open", caller_arguments={"event_id": "e1"}))

    assert [r.url.path for r in upstream.requests] == ["/e/e1", "/e/e2", "/e/e1"]
    sensitive_steps = await _steps(pool, tenant, "cancel")
    assert [s["arguments_redacted"] for s in sensitive_steps] == ['{"event_id": "[redacted]"}'] * 2
    assert sensitive_steps[0]["arguments_hash"] != sensitive_steps[1]["arguments_hash"]
    open_step = (await _steps(pool, tenant, "cancel_open"))[0]
    assert json.loads(open_step["arguments_redacted"]) == {"event_id": "e1"}
    assert other["id"] is not None


@pytest.mark.asyncio
async def test_a_sensitive_param_at_a_body_path_is_redacted_where_it_sits(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "pii", params=[
        _param("phone", body_path="values.0.1", sensitive=True), _param("name", body_path="values.0.0"),
    ])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    await executor.execute_chain(_request(tenant, agent, "pii", caller_arguments={"phone": "+911", "name": "Asha"}))

    assert upstream.bodies() == [{"values": [["Asha", "+911"]]}]  # the real value goes out
    stored = json.loads((await _steps(pool, tenant, "pii"))[0]["arguments_redacted"])
    assert stored == {"values": [["Asha", "[redacted]"]]}


@pytest.mark.asyncio
async def test_a_disconnected_connector_surfaces_as_unavailable_reconnect_required(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    connection = await pool.fetchval(
        "INSERT INTO oauth_connections (tenant_id, provider, status) VALUES ($1, 'google', 'disconnected') RETURNING id",
        tenant["id"],
    )
    try:
        await _insert(pool, tenant, agent, "needs_google", auth_scheme="oauth2_authorization_code",
                      endpoint_url="https://www.googleapis.com/calendar/v3/x", oauth_connection_id=connection)
        upstream = Upstream()
        monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

        response = await executor.execute_chain(_request(tenant, agent, "needs_google"))

        assert (response.chain_status, response.error) == ("unavailable", "reconnect_required")
        assert upstream.requests == []
    finally:
        await pool.execute("DELETE FROM api_side_effect_claims WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN (SELECT id FROM api_chain_runs WHERE tenant_id = $1)", tenant["id"])
        await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM oauth_connections WHERE id = $1", connection)


@pytest.mark.asyncio
async def test_a_response_transform_runs_before_anything_is_persisted(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "proj", method="PATCH", response_transform={"kind": "google_event_projection"},
                  params=[_param("title")])
    event = {"id": "a" * 32, "summary": "PII-NAME", "attendees": [{"email": "pii@example.com"}],
             "start": {"dateTime": "2026-10-01T10:00:00+05:30"}, "end": {"dateTime": "2026-10-01T10:30:00+05:30"}}
    upstream = Upstream(default=(200, event))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(tenant, agent, "proj", caller_arguments={"title": "T"}))

    assert response.data == {"id": "a" * 32, "start": "2026-10-01T10:00:00+05:30",
                             "end": "2026-10-01T10:30:00+05:30", "service": ""}
    stored = (await _steps(pool, tenant, "proj"))[0]["response_redacted"]
    assert "PII" not in stored and "pii@" not in stored


@pytest.mark.asyncio
async def test_a_transform_that_cannot_read_the_response_fails_the_step(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "freebusy", side_effecting=False, params=[
        _param("time_min", body_path="timeMin"), _param("time_max", body_path="timeMax"),
    ], response_transform={"kind": "google_freebusy_slots", "timezone": "UTC", "day_start": "09:00",
                           "day_end": "17:00", "slot_minutes": 30})
    upstream = Upstream(default=(200, {"unexpected": True}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(tenant, agent, "freebusy", caller_arguments={
        "time_min": "2026-10-01T00:00:00+00:00", "time_max": "2026-10-02T00:00:00+00:00"}))

    assert (response.chain_status, response.error) == ("failed", "response_transform_failed")


# ── T22: the caller_id param source ──────────────────────────────────────

CALLEE = "+919812345678"
CAMPAIGN_DID = "+14155550100"


def _call(direction: str, caller: str, called: str) -> dict:
    return {"call_direction": direction, "caller_number": caller, "called_number": called}


@pytest_asyncio.fixture(loop_scope="session")
async def own_did(pool, tenant_agent):
    """The tenant's campaign DID, which is what an outbound call's caller_number is."""
    tenant, _agent = tenant_agent
    await pool.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", CAMPAIGN_DID, tenant["id"])
    yield CAMPAIGN_DID
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", tenant["id"])


def _find_urls(upstream: Upstream) -> list[str]:
    return [str(r.url) for r in upstream.requests]


@pytest.mark.asyncio
async def test_find_booking_looks_up_the_inbound_caller_and_ignores_what_the_model_says(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    upstream = Upstream(default=(200, {"items": []}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **_call("inbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30", "privateExtendedProperty": "yuviz_phone=+19998887777",
                          "caller_phone": "+19998887777"},
    ))

    assert response.chain_status == "success"
    query = upstream.requests[0].url.params
    assert query["privateExtendedProperty"] == f"yuviz_phone={CALLEE}"
    assert "19998887777" not in "".join(_find_urls(upstream))


@pytest.mark.asyncio
async def test_an_outbound_call_looks_up_the_callee_not_the_campaign_did(pool, tenant_agent, own_did, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    upstream = Upstream(default=(200, {"items": []}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **_call("outbound", CAMPAIGN_DID, CALLEE),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))

    assert response.chain_status == "success"
    assert upstream.requests[0].url.params["privateExtendedProperty"] == f"yuviz_phone={CALLEE}"
    assert CAMPAIGN_DID.lstrip("+") not in "".join(_find_urls(upstream))


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    _call("", CALLEE, "+14155550999"),                 # direction missing: an old Conversation
    _call("test", CALLEE, "+14155550999"),             # a browser call
    _call("sideways", CALLEE, "+14155550999"),
    _call("inbound", "", "+14155550999"),              # withheld caller ID
    _call("outbound", "+14155550999", ""),             # outbound with no callee
    _call("inbound", CALLEE, CALLEE),                  # ambiguous legs
    _call("inbound", "anonymous", "+14155550999"),
])
async def test_no_usable_remote_party_fails_the_step_with_zero_upstream_requests(pool, tenant_agent, monkeypatch, call):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    upstream = Upstream(default=(200, {"items": []}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **call, caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))

    assert (response.chain_status, response.error) == ("invalid_argument", "caller_id_unavailable")
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_an_outbound_call_mislabelled_inbound_presents_the_tenants_own_did_and_fails_closed(
    pool, tenant_agent, own_did, monkeypatch,
):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    upstream = Upstream(default=(200, {"items": []}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **_call("inbound", CAMPAIGN_DID, CALLEE),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))

    assert (response.chain_status, response.error) == ("invalid_argument", "caller_id_unavailable")
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_another_tenants_did_is_an_ordinary_caller(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    other = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ('O', $1) RETURNING id", f"o-{uuid.uuid4().hex[:8]}")
    await pool.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", CALLEE, other)
    try:
        await _apply_calendar(pool, tenant, agent)
        upstream = Upstream(default=(200, {"items": []}))
        monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

        response = await executor.execute_chain(_request(
            tenant, agent, "gcal_find_booking", **_call("inbound", CALLEE, CAMPAIGN_DID),
            caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
        ))

        assert response.chain_status == "success" and len(upstream.requests) == 1
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other)


@pytest.mark.asyncio
async def test_the_lookup_keeps_only_events_stored_under_the_remote_party(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)

    def event(event_id: str, phone: str) -> dict:
        return {"id": event_id, "start": {"dateTime": "2026-10-01T10:00:00+05:30"},
                "end": {"dateTime": "2026-10-01T10:30:00+05:30"},
                "extendedProperties": {"private": {"yuviz_preset": "calendar_booking", "yuviz_phone": phone,
                                                   "service": "Dr Rao"}}}

    upstream = Upstream(default=(200, {"items": [event("a" * 32, "+14155550777"), event("b" * 32, CALLEE)]}))
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "gcal_find_booking", **_call("inbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))

    assert [i["id"] for i in response.data["items"]] == ["b" * 32]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, field, sent", [
    ("gupshup", "destination", "919812345678"),        # bare digits
    ("interakt", "fullPhoneNumber", "+919812345678"),  # keeps the plus
    ("meta", "to", "919812345678"),
])
async def test_a_whatsapp_row_addresses_the_remote_party_in_its_providers_shape(
    pool, tenant_agent, monkeypatch, provider, field, sent,
):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, provider)
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await executor.execute_chain(_request(
        tenant, agent, "whatsapp_send_confirmation", **_call("inbound", CALLEE, CAMPAIGN_DID),
        # What a hostile caller talks the model into: none of it can reach the request.
        caller_arguments={"body_1": "a", "body_2": "b", "body_3": "c", field: "+19998887777",
                          "to": "+19998887777", "destination": "+19998887777"},
    ))

    assert response.chain_status == "success"
    request = upstream.requests[0]
    if provider == "gupshup":
        from urllib.parse import parse_qs
        assert parse_qs(request.content.decode())[field] == [sent]
    else:
        assert json.loads(request.content)[field] == sent
    assert "19998887777" not in request.content.decode() + str(request.url)


@pytest.mark.asyncio
async def test_a_whatsapp_row_on_an_outbound_call_goes_to_the_callee(pool, tenant_agent, own_did, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    await executor.execute_chain(_request(
        tenant, agent, "whatsapp_send_confirmation", **_call("outbound", CAMPAIGN_DID, CALLEE),
        caller_arguments={"body_1": "a", "body_2": "b", "body_3": "c"},
    ))

    assert upstream.bodies()[0]["to"] == CALLEE.lstrip("+")


@pytest.mark.asyncio
async def test_the_recipient_never_shows_in_the_persisted_arguments(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    monkeypatch.setattr(executor, "_step_transport", _mock(Upstream()))

    await executor.execute_chain(_request(
        tenant, agent, "whatsapp_send_confirmation", **_call("inbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"body_1": "a", "body_2": "b", "body_3": "c"},
    ))

    stored = (await _steps(pool, tenant, "whatsapp_send_confirmation"))[0]
    assert json.loads(stored["arguments_redacted"])["to"] == "[redacted]"
    assert CALLEE.lstrip("+") not in stored["arguments_redacted"]
    assert json.loads(stored["argument_sources"])["to"] == "caller_id"


@pytest.mark.asyncio
async def test_the_remote_party_is_resolved_once_per_chain_and_only_for_chains_that_use_it(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calls = []
    real = executor._remote_party

    async def counting(tenant_id, request):
        calls.append(tenant_id)
        return await real(tenant_id, request)

    monkeypatch.setattr(executor, "_remote_party", counting)
    monkeypatch.setattr(executor, "_step_transport", _mock(Upstream(default=(200, {"items": [], "calendars": {}}))))

    await executor.execute_chain(_request(
        tenant, agent, "gcal_check_slots", **_call("inbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"time_min": "2026-10-01T00:00:00+05:30", "time_max": "2026-10-02T00:00:00+05:30"},
    ))
    assert calls == []

    await executor.execute_chain(_request(
        tenant, agent, "gcal_cancel", **_call("inbound", CALLEE, CAMPAIGN_DID),
        caller_arguments={"timeMin": "2026-10-01T00:00:00+05:30"},
    ))
    assert len(calls) == 1  # find_booking and cancel share one resolution


def test_only_remote_party_reads_the_requests_call_numbers():
    tree = ast.parse(inspect.getsource(executor))
    readers: set[str] = set()
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and node.attr in ("caller_number", "called_number", "call_direction"):
                    readers.add(function.name)
    # An enumeration that found nothing would pass vacuously.
    assert "_remote_party" in readers
    assert readers <= {"_remote_party"}


# ── T23: confirmation gate, send cap, claim release ──────────────────────

INBOUND = _call("inbound", CALLEE, CAMPAIGN_DID)
BOOK = {"patient_name": "Asha", "service": "Dr Rao",
        "start_time": "2026-10-01T10:00:00+05:30", "end_time": "2026-10-01T10:30:00+05:30"}
FIND = {"timeMin": "2026-10-01T00:00:00+05:30"}


class _Calendar:
    """A small stand-in for Google Calendar: it keeps the events it is sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.events: dict[str, dict] = {}
        self.delete_outcome: Any = 204          # a status, or an exception to raise

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            event = json.loads(request.content)
            self.events[event["id"]] = event
            return httpx.Response(200, json=event)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{**e, "status": "confirmed"} for e in self.events.values()]})
        if request.method == "DELETE":
            if isinstance(self.delete_outcome, Exception):
                raise self.delete_outcome
            if self.delete_outcome == 204:
                self.events.pop(request.url.path.rsplit("/", 1)[1], None)
            return httpx.Response(self.delete_outcome)
        return httpx.Response(200, json={})

    def sent(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


async def _run(tenant, agent, api_name: str, session: str, turn: str, args: dict, call: dict = INBOUND):
    return await executor.execute_chain(_request(
        tenant, agent, api_name, session_id=session, turn_id=turn, caller_arguments=args, **call))


async def _booked(pool, tenant, api_name: str = "gcal_book") -> list[dict]:
    rows = await pool.fetch(
        "SELECT c.* FROM api_side_effect_claims c JOIN custom_apis a ON a.id = c.custom_api_id "
        "WHERE c.tenant_id = $1 AND a.name = $2", tenant["id"], api_name)
    return [dict(r) for r in rows]


async def _book_over_two_turns(tenant, agent, session: str, args: dict = BOOK, call: dict = INBOUND):
    first = await _run(tenant, agent, "gcal_book", session, f"t-{uuid.uuid4().hex[:6]}", args, call)
    second = await _run(tenant, agent, "gcal_book", session, f"t-{uuid.uuid4().hex[:6]}", args, call)
    return first, second


@pytest.mark.asyncio
async def test_the_first_call_reads_back_and_dispatches_nothing_then_a_later_turn_books(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"

    first = await _run(tenant, agent, "gcal_book", session, "turn-1", BOOK)

    assert first.chain_status == "confirmation_required"
    assert first.error == "confirmation_required"
    assert first.data == {}
    assert first.deterministic_response == "To confirm: Asha, Dr Rao, on Thursday 1 October at 10 AM. Shall I book it?"
    assert calendar.requests == []
    assert await _booked(pool, tenant) == []                       # no claim taken
    assert [s["status"] for s in await _steps(pool, tenant, "gcal_book")] == ["confirmation_required"]
    assert (await pool.fetchval("SELECT status FROM api_chain_runs WHERE id = $1", uuid.UUID(first.run_id))) \
        == "confirmation_required"
    assert CALLEE.lstrip("+") not in json.dumps(first.model_dump())  # the read-back never carries the number

    again = await _run(tenant, agent, "gcal_book", session, "turn-1", BOOK)   # the model looping inside one turn
    assert again.chain_status == "confirmation_required" and calendar.requests == []

    booked = await _run(tenant, agent, "gcal_book", session, "turn-2", BOOK)  # the caller said yes
    assert booked.chain_status == "success"
    assert len(calendar.sent("POST")) == 1
    claim = (await _booked(pool, tenant))[0]
    event = json.loads(calendar.sent("POST")[0].content)
    assert event["id"] == claim["id"].hex                                     # the claim row's own id
    assert event["extendedProperties"]["private"] == {
        "service": "Dr Rao", "yuviz_phone": CALLEE, "yuviz_preset": "calendar_booking"}


@pytest.mark.asyncio
async def test_changed_arguments_and_an_expired_read_back_both_ask_again(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"

    await _run(tenant, agent, "gcal_book", session, "turn-1", BOOK)
    later = {**BOOK, "start_time": "2026-10-01T11:00:00+05:30", "end_time": "2026-10-01T11:30:00+05:30"}
    changed = await _run(tenant, agent, "gcal_book", session, "turn-2", later)
    assert changed.chain_status == "confirmation_required" and "11 AM" in changed.deterministic_response

    await pool.execute(
        "UPDATE api_chain_steps SET created_at = now() - interval '11 minutes' WHERE run_id IN "
        "(SELECT id FROM api_chain_runs WHERE tenant_id = $1)", tenant["id"])
    expired = await _run(tenant, agent, "gcal_book", session, "turn-3", BOOK)
    assert expired.chain_status == "confirmation_required"
    assert calendar.requests == []


@pytest.mark.asyncio
async def test_another_tenants_pending_read_back_does_not_confirm(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    other = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ('O', $1) RETURNING id", f"o-{uuid.uuid4().hex[:8]}")
    other_agent = await pool.fetchval("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 's', 'S') RETURNING id", other)
    try:
        await _apply_calendar(pool, tenant, agent)
        calendar = _Calendar()
        monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
        session = f"s-{uuid.uuid4().hex[:8]}"

        first = await _run(tenant, agent, "gcal_book", session, "turn-1", BOOK)
        # The same pending read-back, same session, same api and hash, but another tenant's run.
        await pool.execute("UPDATE api_chain_runs SET tenant_id = $2, agent_id = $3 WHERE id = $1",
                           uuid.UUID(first.run_id), other, other_agent)

        second = await _run(tenant, agent, "gcal_book", session, "turn-2", BOOK)

        assert second.chain_status == "confirmation_required"
        assert calendar.requests == []
    finally:
        await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN (SELECT id FROM api_chain_runs WHERE tenant_id = $1)", other)
        await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other)


@pytest.mark.asyncio
async def test_without_a_session_nothing_can_be_confirmed(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))

    for turn in ("turn-1", "turn-2"):
        response = await _run(tenant, agent, "gcal_book", "", turn, BOOK)
        assert response.chain_status == "confirmation_required"
    assert calendar.requests == []


@pytest.mark.asyncio
async def test_a_template_that_cannot_be_rendered_fails_without_dispatching(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _insert(pool, tenant, agent, "gated", confirmation_template="Do {{$.missing}}?", params=[_param("x")])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await _run(tenant, agent, "gated", "s1", "turn-1", {"x": "1"})

    assert (response.chain_status, response.error) == ("failed", "confirmation_unrenderable")
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_cancel_reads_back_the_matched_appointment_and_deletes_only_after_a_later_turn(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"
    await _book_over_two_turns(tenant, agent, session)

    first = await _run(tenant, agent, "gcal_cancel", session, "turn-c1", FIND)
    assert first.chain_status == "confirmation_required"
    assert first.deterministic_response == "To confirm, cancel your Dr Rao appointment on Thursday 1 October at 10 AM?"
    assert calendar.sent("DELETE") == []
    assert [s.status for s in first.steps] == ["success", "confirmation_required"]   # the lookup ran, the cancel did not

    second = await _run(tenant, agent, "gcal_cancel", session, "turn-c2", FIND)
    assert second.chain_status == "success"
    assert len(calendar.sent("DELETE")) == 1


@pytest.mark.asyncio
async def test_cancel_then_rebook_works_and_the_rebook_gets_a_fresh_event_id(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"
    await _book_over_two_turns(tenant, agent, session)
    first_id = (await _booked(pool, tenant))[0]["id"]
    await _run(tenant, agent, "gcal_cancel", session, "turn-c1", FIND)
    await _run(tenant, agent, "gcal_cancel", session, "turn-c2", FIND)
    assert await _booked(pool, tenant) == []                         # the cancelled booking's claim is gone

    prompt, rebooked = await _book_over_two_turns(tenant, agent, session)

    # The old read-back is retired by the booking it served, so the rebook asks again.
    assert prompt.chain_status == "confirmation_required"
    assert rebooked.chain_status == "success"
    new_id = (await _booked(pool, tenant))[0]["id"]
    assert new_id != first_id
    assert [json.loads(r.content)["id"] for r in calendar.sent("POST")] == [first_id.hex, new_id.hex]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [404, httpx.ReadTimeout("slow")])
async def test_a_failed_cancel_releases_nothing(pool, tenant_agent, monkeypatch, outcome):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"
    await _book_over_two_turns(tenant, agent, session)
    calendar.delete_outcome = outcome

    await _run(tenant, agent, "gcal_cancel", session, "turn-c1", FIND)
    cancelled = await _run(tenant, agent, "gcal_cancel", session, "turn-c2", FIND)

    assert cancelled.chain_status != "success"
    claims = await _booked(pool, tenant)
    assert [c["status"] for c in claims] == ["success"]
    _, rebook = await _book_over_two_turns(tenant, agent, session)
    assert (rebook.chain_status, rebook.error) == ("failed", "side_effecting_step_already_completed")


@pytest.mark.asyncio
async def test_a_release_error_still_reports_the_cancel_as_a_success_and_logs_no_identifiers(
    pool, tenant_agent, monkeypatch, caplog,
):
    tenant, agent = tenant_agent
    await _apply_calendar(pool, tenant, agent)
    calendar = _Calendar()
    monkeypatch.setattr(executor, "_step_transport", _mock(calendar))
    session = f"s-{uuid.uuid4().hex[:8]}"
    await _book_over_two_turns(tenant, agent, session)
    event_id = (await _booked(pool, tenant))[0]["id"].hex

    async def _broken(*args, **kwargs):
        raise RuntimeError(f"db down while releasing {event_id}")

    monkeypatch.setattr(executor, "_release_booking_claim", _broken)
    await _run(tenant, agent, "gcal_cancel", session, "turn-c1", FIND)
    with caplog.at_level("WARNING"):
        cancelled = await _run(tenant, agent, "gcal_cancel", session, "turn-c2", FIND)

    assert cancelled.chain_status == "success"
    assert len(calendar.sent("DELETE")) == 1
    assert [r.getMessage() for r in caplog.records if "release" in r.getMessage()] == ["booking_claim_release_failed"]
    assert event_id not in caplog.text and "db down" not in caplog.text


async def _claim(pool, tenant_id, api_id, run_id, status: str = "success") -> uuid.UUID:
    return await pool.fetchval(
        "INSERT INTO api_side_effect_claims (tenant_id, custom_api_id, arguments_hash, run_id, session_id, status) "
        "VALUES ($1, $2, $3, $4, 's', $5) RETURNING id", tenant_id, api_id, f"h-{uuid.uuid4().hex}", run_id, status)


@pytest.mark.asyncio
async def test_a_release_only_frees_a_success_claim_of_the_named_booking_row_in_this_tenant(pool, tenant_agent):
    tenant, agent = tenant_agent
    rows = await _apply_calendar(pool, tenant, agent)
    other = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ('O', $1) RETURNING id", f"o-{uuid.uuid4().hex[:8]}")
    try:
        run = await pool.fetchval(
            "INSERT INTO api_chain_runs (tenant_id, agent_id, call_id, session_id, turn_id, tool_call_id, "
            "idempotency_key, target_api_id, status) VALUES ($1, $2, 'c', 's', 't', 'tc', $3, $4, 'success') RETURNING id",
            tenant["id"], agent["id"], f"k-{uuid.uuid4().hex}", rows["gcal_book"]["id"])
        set_target_tenant(str(other))
        async with tenant_conn(pool) as conn:
            async with conn.transaction():
                other_book = await custom_apis._insert_custom_api(
                    conn, tenant_id=other, name="gcal_book", description="d", endpoint_url="https://o.example.com/x",
                    method="POST", body_style="json", auth_scheme="none", auth_config={}, side_effecting=True,
                    idempotency_header=None, timeout_ms=None, sensitive_response_paths=[], success_template=None,
                    params=[], preset_key="calendar_booking")
        set_target_tenant(str(tenant["id"]))
        release = lambda who, claim: executor._release_booking_claim(  # noqa: E731
            str(who), "calendar_booking", "gcal_book", claim.hex)

        own = await _claim(pool, tenant["id"], rows["gcal_book"]["id"], run)
        assert await release(other, own) is False                      # the wrong tenant names it
        assert await release(tenant["id"], own) is True                # the right one frees it
        assert await pool.fetchval("SELECT count(*) FROM api_side_effect_claims WHERE id = $1", own) == 0

        # Each tenant predicate on its own: a claim stamped with one tenant but
        # pointing at the other's booking row must not be freed by either.
        stamped_other = await _claim(pool, other, rows["gcal_book"]["id"], run)
        points_at_other = await _claim(pool, tenant["id"], other_book["id"], run)
        assert await release(tenant["id"], stamped_other) is False
        assert await release(tenant["id"], points_at_other) is False

        # Only a success claim, and only the booking row named.
        in_flight = await _claim(pool, tenant["id"], rows["gcal_book"]["id"], run, status="claimed")
        assert await release(tenant["id"], in_flight) is False
        not_a_booking = await _claim(pool, tenant["id"], rows["gcal_cancel"]["id"], run)
        assert await release(tenant["id"], not_a_booking) is False

        with pytest.raises(ValueError):                                # an event made by hand in Google
            await executor._release_booking_claim(str(tenant["id"]), "calendar_booking", "gcal_book", "not-a-uuid")
    finally:
        await pool.execute("DELETE FROM api_side_effect_claims WHERE tenant_id = ANY($1::uuid[])", [tenant["id"], other])
        await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", tenant["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other)
        set_target_tenant(str(tenant["id"]))


async def _send(tenant, agent, session: str, n: int, call: dict = INBOUND):
    return await _run(tenant, agent, "whatsapp_send_confirmation", session, f"t{n}",
                      {"body_1": f"v{n}", "body_2": "b", "body_3": "c"}, call)


@pytest.mark.asyncio
async def test_a_session_gets_three_sends_and_the_fourth_takes_no_claim_and_no_request(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))
    session = f"s-{uuid.uuid4().hex[:8]}"

    results = [await _send(tenant, agent, session, n) for n in range(4)]

    assert [r.chain_status for r in results] == ["success", "success", "success", "unavailable"]
    assert results[3].error == "send_cap_reached"        # `unavailable`, which does not invite a retry
    assert len(upstream.requests) == 3
    assert len(await _booked(pool, tenant, "whatsapp_send_confirmation")) == 3

    other_session = await _send(tenant, agent, f"s-{uuid.uuid4().hex[:8]}", 9)
    assert other_session.chain_status == "success"


@pytest.mark.asyncio
async def test_a_null_cap_lets_the_fourth_send_through(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    await pool.execute("UPDATE custom_apis SET session_send_cap = NULL WHERE tenant_id = $1", tenant["id"])
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))
    session = f"s-{uuid.uuid4().hex[:8]}"

    results = [await _send(tenant, agent, session, n) for n in range(4)]

    assert [r.chain_status for r in results] == ["success"] * 4 and len(upstream.requests) == 4


@pytest.mark.asyncio
async def test_a_send_with_no_session_fails_closed(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))

    response = await _send(tenant, agent, "", 0)

    assert (response.chain_status, response.error) == ("unavailable", "send_cap_reached")
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_another_tenants_sends_do_not_count_against_this_sessions_cap(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    await _apply_whatsapp(pool, tenant, agent, "meta")
    upstream = Upstream()
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))
    session = f"s-{uuid.uuid4().hex[:8]}"
    other = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ('O', $1) RETURNING id", f"o-{uuid.uuid4().hex[:8]}")
    other_agent = await pool.fetchval("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 's', 'S') RETURNING id", other)
    try:
        api_id = (await pool.fetchrow("SELECT id FROM custom_apis WHERE tenant_id = $1", tenant["id"]))["id"]
        for n in range(3):
            # Three successful sends of this very row in this very session, recorded under another tenant.
            run = await pool.fetchval(
                "INSERT INTO api_chain_runs (tenant_id, agent_id, call_id, session_id, turn_id, tool_call_id, "
                "idempotency_key, target_api_id, status) VALUES ($1, $2, 'c', $3, $4, 't', $5, $6, 'success') RETURNING id",
                other, other_agent, session, f"x{n}", f"k-{uuid.uuid4().hex}", api_id)
            await pool.execute(
                "INSERT INTO api_chain_steps (run_id, step_index, custom_api_id, api_name, level, session_id, status, "
                "side_effecting, arguments_hash) VALUES ($1, 0, $2, 'whatsapp_send_confirmation', 1, $3, 'success', "
                "true, $4)", run, api_id, session, f"h{n}")

        response = await _send(tenant, agent, session, 0)

        assert response.chain_status == "success"
    finally:
        await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN (SELECT id FROM api_chain_runs WHERE tenant_id = $1)", other)
        await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other)
