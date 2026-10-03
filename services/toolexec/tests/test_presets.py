"""
Pure tests for services/toolexec/presets.py: the definitions, the remote-party
rule, the response transforms and the confirmation read-back. No database.
"""

from __future__ import annotations

import json

import pytest

from services.toolexec import oauth, presets, redaction
from services.toolexec.schemas import CalendarBookingSetup, SheetsLeadCaptureSetup, WhatsAppSetup

P1 = "+919812345678"
P2 = "+14155550123"


def _whatsapp(provider: str, **extra) -> WhatsAppSetup:
    return WhatsAppSetup(preset_key="whatsapp_confirmation", provider=provider, api_key="k", template="tpl", **extra)


def _all_steps() -> list[presets.PresetStep]:
    steps = presets.PRESETS["calendar_booking"].steps(CalendarBookingSetup(preset_key="calendar_booking"))
    steps += presets.PRESETS["whatsapp_confirmation"].steps(
        _whatsapp("gupshup", source_number="919800000000", app_name="clinic"))
    steps += presets.PRESETS["whatsapp_confirmation"].steps(_whatsapp("interakt"))
    steps += presets.PRESETS["whatsapp_confirmation"].steps(_whatsapp("meta", phone_number_id="123456789"))
    steps += presets.PRESETS["sheets_lead_capture"].steps(
        SheetsLeadCaptureSetup(preset_key="sheets_lead_capture"), spreadsheet_id="sheet-1")
    return steps


def _step(name: str) -> presets.PresetStep:
    return next(s for s in _all_steps() if s.name == name)


# ── definitions ───────────────────────────────────────────────────────────

def test_the_enumeration_covers_every_row_the_design_names():
    names = [s.name for s in _all_steps()]
    # Five calendar rows, one WhatsApp row per provider, one Sheets row. A new
    # row or provider changes this count and has to be looked at.
    assert names == [
        "gcal_check_slots", "gcal_book", "gcal_find_booking", "gcal_reschedule", "gcal_cancel",
        "whatsapp_send_confirmation", "whatsapp_send_confirmation", "whatsapp_send_confirmation",
        "sheets_capture_lead",
    ]


def test_no_param_is_named_upstream():
    # render_confirmation reads {**arguments, "upstream": responses}; a param of that name would shadow it.
    params = [p for s in _all_steps() for p in s.params]
    assert len(params) > 30
    assert not [p for p in params if p.name == "upstream"]


def test_every_caller_id_param_is_sensitive():
    caller_id = [(s.name, p) for s in _all_steps() for p in s.params if p.source == "caller_id"]
    assert len(caller_id) == 5  # book, find, and one recipient per WhatsApp provider
    assert all(p.sensitive for _name, p in caller_id)


def test_whatsapp_rows_cap_sends_and_address_the_caller():
    rows = [s for s in _all_steps() if s.name == "whatsapp_send_confirmation"]
    assert len(rows) == 3
    for row in rows:
        assert row.session_send_cap == 3 and row.side_effecting
        # The recipient is the only way the row names a number, and it is server-chosen.
        recipients = [p for p in row.params if p.source == "caller_id"]
        assert len(recipients) == 1
        assert not [p for p in row.params if p.source == "caller" and p.name == recipients[0].name]

    gupshup, interakt, meta = rows
    recipient = lambda row: next(p for p in row.params if p.source == "caller_id")  # noqa: E731
    assert (recipient(gupshup).name, recipient(gupshup).value_digits_only) == ("destination", True)
    assert (recipient(meta).name, recipient(meta).value_digits_only) == ("to", True)
    assert (recipient(interakt).name, recipient(interakt).value_digits_only) == ("fullPhoneNumber", False)


def test_sheets_row_appends_raw():
    row = _step("sheets_capture_lead")
    value_input = next(p for p in row.params if p.name == "valueInputOption")
    assert (value_input.source, value_input.literal_value, value_input.location) == ("literal", "RAW", "query")


def test_find_booking_has_no_free_text_query_param():
    row = _step("gcal_find_booking")
    assert "q" not in {p.name for p in row.params}
    lookup = next(p for p in row.params if p.source == "caller_id")
    assert (lookup.name, lookup.value_prefix) == ("privateExtendedProperty", "yuviz_phone=")


def test_gated_rows_are_side_effecting():
    # custom_apis_confirmation_shape / _send_cap_shape: the gate and the cap need a claim hash.
    for step in _all_steps():
        if step.confirmation_template or step.session_send_cap:
            assert step.side_effecting, step.name
    assert {s.name for s in _all_steps() if s.confirmation_template} == {"gcal_book", "gcal_reschedule", "gcal_cancel"}


def test_every_transform_a_row_carries_is_valid():
    carried = [s.response_transform for s in _all_steps() if s.response_transform]
    assert len(carried) == 3
    for transform in carried:
        presets.validate_response_transform(transform)


def test_oauth_scopes_come_from_the_preset_definitions():
    oauth_presets = {key: p for key, p in presets.PRESETS.items() if p.provider}
    assert set(oauth_presets) == {"calendar_booking", "sheets_lead_capture"}
    for preset in oauth_presets.values():
        assert preset.provider in oauth.PROVIDERS and preset.scopes
    assert not hasattr(oauth, "_PRESET_SCOPES")


# ── the caller-id param source ────────────────────────────────────────────

@pytest.mark.parametrize("direction, caller, called, expected", [
    ("inbound", P1, P2, P1),
    ("outbound", P2, P1, P1),                      # outbound: the callee, never the tenant's own DID
    ("inbound", "", P2, None),
    ("outbound", P2, "", None),
    ("", P1, P2, None),                            # direction missing
    ("test", P1, P2, None),                        # a browser call
    ("sideways", P1, P2, None),
    ("INBOUND", P1, P2, None),
    ("inbound", P1, P1, None),                     # equal legs are ambiguous
    ("outbound", "+91 98123 45678", P1, None),     # equal after normalizing
    ("inbound", "12345", P2, None),                # too short to be a number
    ("inbound", "anonymous", P2, None),
    ("inbound", "+91 (98123) 45-678", P2, P1),
])
def test_remote_party_number(direction, caller, called, expected):
    assert presets.remote_party_number(direction, caller, called) == expected


def test_normalize_ani():
    assert presets.normalize_ani("+91 98123-45678") == P1
    assert presets.normalize_ani("919812345678") == P1
    assert presets.normalize_ani("1234567") is None
    assert presets.normalize_ani("1" * 16) is None
    assert presets.normalize_ani("") is None


# ── response transforms ───────────────────────────────────────────────────

def _freebusy_config(**overrides) -> dict:
    return {"kind": "google_freebusy_slots", "timezone": "Asia/Kolkata", "day_start": "09:00",
            "day_end": "12:00", "slot_minutes": 30, **overrides}


def test_freebusy_subtracts_busy_time_and_speaks_the_first_three():
    response = {"calendars": {"primary": {"busy": [
        {"start": "2026-10-01T09:00:00+05:30", "end": "2026-10-01T10:00:00+05:30"},
    ]}}}
    body = {"timeMin": "2026-10-01T00:00:00+05:30", "timeMax": "2026-10-02T00:00:00+05:30"}
    result = presets.apply_response_transform(_freebusy_config(), response, body)
    assert [s["spoken"] for s in result["slots"]] == ["10 AM", "10:30 AM", "11 AM"]
    assert result["spoken_slots"] == "10 AM, 10:30 AM, or 11 AM"
    assert result["slot_count"] == 3


def test_freebusy_clips_to_the_requested_window_and_reports_none():
    body = {"timeMin": "2026-10-01T11:45:00+05:30", "timeMax": "2026-10-01T12:00:00+05:30"}
    result = presets.apply_response_transform(_freebusy_config(), {"calendars": {"primary": {"busy": []}}}, body)
    assert result == {"slots": [], "slot_count": 0, "spoken_slots": "none in that window"}


def test_freebusy_names_the_weekday_when_the_slots_span_days():
    body = {"timeMin": "2026-10-01T11:00:00+05:30", "timeMax": "2026-10-02T23:00:00+05:30"}
    result = presets.apply_response_transform(
        _freebusy_config(), {"calendars": {"primary": {"busy": []}}}, body)
    assert result["spoken_slots"] == "Thursday 11 AM, Thursday 11:30 AM, or Friday 9 AM"


def test_freebusy_fails_closed_when_google_could_not_read_the_calendar():
    body = {"timeMin": "2026-10-01T00:00:00+05:30", "timeMax": "2026-10-02T00:00:00+05:30"}
    response = {"calendars": {"primary": {"errors": [{"reason": "notFound"}], "busy": []}}}
    with pytest.raises(ValueError):
        presets.apply_response_transform(_freebusy_config(), response, body)


def _event(**overrides) -> dict:
    event = {
        "id": "a" * 32, "status": "confirmed", "summary": "PII-NAME Asha Rao",
        "description": "PII-DESC notes", "attendees": [{"email": "pii-attendee@example.com"}],
        "creator": {"email": "pii-creator@example.com"}, "htmlLink": "https://pii.example/event",
        "start": {"dateTime": "2026-10-01T10:00:00+05:30"}, "end": {"dateTime": "2026-10-01T10:30:00+05:30"},
        "extendedProperties": {"private": {
            "yuviz_preset": "calendar_booking", "yuviz_phone": P1, "service": "Dr Rao",
        }},
    }
    event.update(overrides)
    return event


def _lookup(*events, ani=P1) -> dict:
    return presets.apply_response_transform(
        {"kind": "google_booking_lookup"}, {"items": list(events)}, {}, caller_ani=ani)


def test_lookup_keeps_a_matching_event_and_projects_it():
    result = _lookup(_event())
    assert result == {"items": [{"id": "a" * 32, "start": "2026-10-01T10:00:00+05:30",
                                 "end": "2026-10-01T10:30:00+05:30", "service": "Dr Rao"}], "match_count": 1}


def test_lookup_drops_an_event_failing_any_one_check():
    private = lambda **kw: {"private": {"yuviz_preset": "calendar_booking", "yuviz_phone": P1, **kw}}  # noqa: E731
    bad = [
        _event(extendedProperties={"private": {"yuviz_phone": P1}}),                    # phone matches, no preset
        _event(extendedProperties=private(yuviz_preset="other")),
        _event(extendedProperties=private(yuviz_phone=P2)),                             # someone else's
        _event(id="not-a-uuid-hex"),
        _event(id="A" * 32),                                                            # not a claim id
        _event(id="a" * 31),
        _event(status="cancelled"),
        _event(extendedProperties={}),
    ]
    for event in bad:
        assert _lookup(event) == {"items": [], "match_count": 0}, event["id"]


def test_lookup_keeps_only_the_earliest_and_counts_matches():
    later = _event(id="b" * 32, start={"dateTime": "2026-10-03T10:00:00+05:30"})
    earlier = _event(id="c" * 32, start={"dateTime": "2026-10-02T10:00:00+05:30"})
    result = _lookup(later, earlier)
    assert [i["id"] for i in result["items"]] == ["c" * 32] and result["match_count"] == 2


def test_lookup_with_no_caller_returns_nothing():
    assert _lookup(_event(), ani=None) == {"items": [], "match_count": 0}


def test_no_planted_pii_survives_a_projection():
    projected = [
        _lookup(_event()),
        presets.apply_response_transform({"kind": "google_event_projection"}, _event(), {}),
    ]
    for result in projected:
        text = json.dumps(result)
        for sentinel in ("PII-NAME", "PII-DESC", "pii-attendee", "pii-creator", "pii.example", P1):
            assert sentinel not in text
        assert set(result["items"][0] if "items" in result else result) == {"id", "start", "end", "service"}


def test_validate_response_transform_is_a_closed_set():
    presets.validate_response_transform({"kind": "google_booking_lookup"})
    for bad in ({"kind": "jq"}, {"kind": "google_booking_lookup", "extra": 1}, {"kind": "google_freebusy_slots"},
                {}, None, "google_booking_lookup"):
        with pytest.raises(ValueError):
            presets.validate_response_transform(bad)


# ── confirmation read-back ────────────────────────────────────────────────

def _book_arguments() -> dict:
    return {
        "summary": "Asha", "start": {"dateTime": "2026-10-01T10:00:00+05:30"},
        "extendedProperties": {"private": {"service": "Dr Rao", "yuviz_phone": redaction.REDACTED}},
    }


def test_confirmation_reads_back_the_exact_arguments_with_the_instant_spoken():
    book = next(s for s in _all_steps() if s.name == "gcal_book")
    text = presets.render_confirmation(book.confirmation_template, _book_arguments(), {})
    assert text == "To confirm: Asha, Dr Rao, on Thursday 1 October at 10 AM. Shall I book it?"


def test_confirmation_reads_the_matched_appointment_from_the_lookup():
    cancel = next(s for s in _all_steps() if s.name == "gcal_cancel")
    upstream = {"gcal_find_booking": {"items": [{"id": "a" * 32, "service": "Dr Rao",
                                                 "start": "2026-10-01T15:30:00+05:30", "end": "x"}]}}
    assert presets.render_confirmation(cancel.confirmation_template, {"event_id": "a" * 32}, upstream) == (
        "To confirm, cancel your Dr Rao appointment on Thursday 1 October at 3:30 PM?")


def test_confirmation_refuses_a_missing_or_redacted_placeholder():
    with pytest.raises(ValueError, match="confirmation_unrenderable"):
        presets.render_confirmation("Hello {{$.nope}}", {}, {})
    with pytest.raises(ValueError, match="confirmation_unrenderable"):
        presets.render_confirmation("Phone {{$.phone}}", {"phone": redaction.REDACTED}, {})


def test_confirmation_strips_control_characters_from_spoken_values():
    assert presets.render_confirmation("Hi {{$.name}}", {"name": "A\r\nB"}, {}) == "Hi AB"


def test_claim_release_target():
    assert presets.claim_release_target({"preset_key": "calendar_booking", "name": "gcal_cancel"}) == "gcal_book"
    assert presets.claim_release_target({"preset_key": "calendar_booking", "name": "gcal_book"}) is None
    assert presets.claim_release_target({"preset_key": None, "name": "gcal_cancel"}) is None
    assert presets.claim_release_target({"preset_key": "whatsapp_confirmation", "name": "gcal_cancel"}) is None
