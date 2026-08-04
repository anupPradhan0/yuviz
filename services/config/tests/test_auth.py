from __future__ import annotations

import time

import jwt
import pytest

from services.config import auth


def test_hash_and_verify_password_roundtrip():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed)
    assert not auth.verify_password("wrong password", hashed)


def test_password_hash_is_not_the_plaintext():
    hashed = auth.hash_password("hunter2")
    assert hashed != "hunter2"
    assert hashed.startswith("$2b$")  # bcrypt's own format marker


def test_create_and_decode_access_token_roundtrip():
    user = {"id": "11111111-1111-1111-1111-111111111111", "email": "a@example.com", "role": "admin", "tenant_id": None}
    token = auth.create_access_token(user)
    decoded = auth.decode_access_token(token)
    assert decoded.id == user["id"]
    assert decoded.email == user["email"]
    assert decoded.role == "admin"
    assert decoded.tenant_id is None


def test_decode_rejects_tampered_token():
    user = {"id": "1", "email": "a@example.com", "role": "admin", "tenant_id": None}
    token = auth.create_access_token(user)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(auth.InvalidTokenError):
        auth.decode_access_token(tampered)


def test_decode_rejects_expired_token():
    user = {"id": "1", "email": "a@example.com", "role": "admin", "tenant_id": None}
    # Build an already-expired token directly, bypassing create_access_token's
    # fixed TTL, to test the expiry check itself deterministically.
    payload = {
        "sub": user["id"], "email": user["email"], "role": user["role"], "tenant_id": None,
        "iat": int(time.time()) - 100, "exp": int(time.time()) - 1,
    }
    expired = jwt.encode(payload, auth.JWT_SECRET, algorithm=auth.JWT_ALGORITHM)
    with pytest.raises(auth.InvalidTokenError):
        auth.decode_access_token(expired)


def test_decode_rejects_wrong_secret():
    payload = {
        "sub": "1", "email": "a@example.com", "role": "admin", "tenant_id": None,
        "iat": int(time.time()), "exp": int(time.time()) + 3600,
    }
    forged = jwt.encode(
        payload, "not-the-real-secret-but-long-enough-for-hs256",
        algorithm=auth.JWT_ALGORITHM,
    )
    with pytest.raises(auth.InvalidTokenError):
        auth.decode_access_token(forged)
