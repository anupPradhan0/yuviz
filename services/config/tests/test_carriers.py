"""
Carriers are the fourth ref table (connector-presets design, round-4 finding
2): the plaintext `auth_token` is sealed server-side, and an `enc:` ref a
client presents is accepted only if it is the one already stored.
"""

from __future__ import annotations

import json

import pytest

from libs.config_sdk.secrets import decrypt_secret, generate_key
from services.config import carriers

PLAINTEXT = "plivo-token-PLAINTEXT-sentinel"


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


async def _create(test_tenant, **kwargs):
    return await carriers.create_carrier(
        tenant_id=test_tenant["id"], name="BYOC", provider="plivo",
        allow_pointer_schemes=False, **kwargs,
    )


async def test_plaintext_auth_token_is_stored_sealed(test_tenant, scoped):
    created = await _create(test_tenant, auth_token=PLAINTEXT)
    assert created["auth_token_ref"].startswith("enc:")
    assert PLAINTEXT not in created["auth_token_ref"]
    assert decrypt_secret(created["auth_token_ref"]) == PLAINTEXT


async def test_audit_row_for_create_holds_no_plaintext_token(test_tenant, scoped, pool):
    created = await _create(test_tenant, auth_token=PLAINTEXT)
    row = await pool.fetchrow(
        "SELECT * FROM audit_log WHERE entity_type = 'carrier' AND entity_id = $1 "
        "ORDER BY changed_at DESC LIMIT 1",
        created["id"],
    )
    assert PLAINTEXT not in row["new_value"]
    assert json.loads(row["new_value"])["auth_token_ref"] == "[redacted]"


async def test_pasting_another_tenants_ciphertext_on_create_is_refused(test_tenant, scoped):
    first = await _create(test_tenant, auth_token=PLAINTEXT)
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        await _create(test_tenant, auth_token_ref=first["auth_token_ref"])


async def test_update_keeps_the_stored_value_on_stored_sentinel_and_byte_identical_roundtrip(test_tenant, scoped):
    created = await _create(test_tenant, auth_token=PLAINTEXT)
    for echoed in ("[stored]", created["auth_token_ref"]):
        updated = await carriers.update_carrier(
            created["id"], allow_pointer_schemes=False, auth_token_ref=echoed, name="renamed",
        )
        assert updated["auth_token_ref"] == created["auth_token_ref"]


async def test_update_refuses_a_ciphertext_the_row_does_not_hold(test_tenant, scoped):
    mine = await _create(test_tenant, auth_token="mine")
    theirs = await _create(test_tenant, auth_token="theirs")
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        await carriers.update_carrier(
            mine["id"], allow_pointer_schemes=False, auth_token_ref=theirs["auth_token_ref"],
        )
    row = await carriers.get_carrier_by_id(mine["id"])
    assert decrypt_secret(row["auth_token_ref"]) == "mine"


async def test_update_with_a_new_plaintext_token_rotates_it(test_tenant, scoped):
    created = await _create(test_tenant, auth_token=PLAINTEXT)
    updated = await carriers.update_carrier(created["id"], allow_pointer_schemes=False, auth_token="rotated")
    assert decrypt_secret(updated["auth_token_ref"]) == "rotated"


async def test_pointer_scheme_only_for_platform_callers(test_tenant, scoped):
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        await _create(test_tenant, auth_token_ref="env:PLIVO_AUTH_TOKEN")
    created = await carriers.create_carrier(
        tenant_id=test_tenant["id"], name="BYOC", provider="plivo",
        auth_token_ref="env:PLIVO_AUTH_TOKEN", allow_pointer_schemes=True,
    )
    assert created["auth_token_ref"] == "env:PLIVO_AUTH_TOKEN"


def test_public_carrier_masks_only_when_asked():
    row = {"auth_token_ref": "enc:abc", "name": "x"}
    assert carriers.public_carrier(row, masked=True) == {"auth_token_ref": "[stored]", "name": "x"}
    assert carriers.public_carrier(row, masked=False) is row
