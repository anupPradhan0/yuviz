"""
Tenant-bound ciphertext: a ref sealed for one tenant must not open for
another, and the shared Fernet reader must refuse it outright.
"""

from __future__ import annotations

import base64
import uuid

import asyncpg.pgproto.pgproto as pgproto
import pytest

from libs.config_sdk.secrets import (
    SecretTenantMismatch,
    decrypt_secret,
    decrypt_tenant_secret,
    encrypt_secret,
    encrypt_tenant_secret,
    generate_key,
    is_tenant_bound,
)

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


def test_opens_for_its_own_tenant_as_str_and_as_asyncpg_uuid():
    ref = encrypt_tenant_secret(str(TENANT_A), "s3cret")
    assert is_tenant_bound(ref)
    assert decrypt_tenant_secret(str(TENANT_A), ref) == "s3cret"
    assert decrypt_tenant_secret(pgproto.UUID(str(TENANT_A)), ref) == "s3cret"


def test_does_not_open_for_another_tenant():
    ref = encrypt_tenant_secret(TENANT_A, "s3cret")
    with pytest.raises(SecretTenantMismatch) as exc:
        decrypt_tenant_secret(TENANT_B, ref)
    assert ref not in str(exc.value)


def test_a_flipped_byte_raises_mismatch():
    ref = encrypt_tenant_secret(TENANT_A, "s3cret")
    raw = bytearray(base64.urlsafe_b64decode(ref[len("enc:t1."):]))
    raw[-1] ^= 1
    tampered = "enc:t1." + base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(SecretTenantMismatch):
        decrypt_tenant_secret(TENANT_A, tampered)


def test_bad_base64_raises_mismatch():
    with pytest.raises(SecretTenantMismatch):
        decrypt_tenant_secret(TENANT_A, "enc:t1.!!!not-base64!!!")


def test_non_uuid_tenant_raises_value_error():
    with pytest.raises(ValueError):
        encrypt_tenant_secret("not-a-uuid", "s3cret")


def test_empty_plaintext_raises():
    with pytest.raises(ValueError):
        encrypt_tenant_secret(TENANT_A, "")


def test_legacy_ref_is_not_opened_as_tenant_bound():
    with pytest.raises(ValueError) as exc:
        decrypt_tenant_secret(TENANT_A, encrypt_secret("legacy"))
    assert not isinstance(exc.value, SecretTenantMismatch)


def test_shared_decrypt_refuses_a_tenant_bound_ref():
    with pytest.raises(ValueError):
        decrypt_secret(encrypt_tenant_secret(TENANT_A, "s3cret"))


def test_legacy_fernet_round_trip_still_works():
    assert decrypt_secret(encrypt_secret("legacy")) == "legacy"
