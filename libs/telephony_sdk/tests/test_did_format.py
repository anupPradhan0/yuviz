import pytest

from libs.telephony_sdk.did_format import normalize_did


@pytest.mark.parametrize("raw, canonical", [
    ("+14165550177", "14165550177"),
    ("14165550177", "14165550177"),
    ("+1 (416) 555-0177", "14165550177"),
    (" +91.80.6535.4620 ", "918065354620"),
    ("+918065354620\t\n", "918065354620"),                # same whitespace the backfill trims
    ("5008", "5008"),                                  # native extensions are already canonical
    ("0091 80 6535", "0091806535"),                    # leading zeros survive
    ("sip:101@asterisk.local", "sip:101@asterisk.local"),  # SIP URIs are kept as typed
    ("sip:5003@x", "sip:5003@x"),                      # never collapses to a platform extension
    ("sip:desk@pbx.example.com", "sip:desk@pbx.example.com"),
    ("frontdesk", "frontdesk"),
    ("+", "+"),                                        # no digit: not a phone number, left alone
    ("", ""),
    (None, ""),
])
def test_normalize_did_canonicalises_phone_numbers_only(raw, canonical):
    assert normalize_did(raw) == canonical


@pytest.mark.parametrize("did, routable", [
    ("918065354620", True), ("5008", True), ("sip:101@pbx", True), ("SIPS:a@b", True), ("frontdesk", True),
    ("tel:+918065354620", False), ("ext101", False), ("+91 80\u20136535", False), ("\u0665\u0660\u0660\u0668", False),
])
def test_is_routable_did(did, routable):
    from libs.telephony_sdk.did_format import is_routable_did

    assert is_routable_did(normalize_did(did)) is routable
