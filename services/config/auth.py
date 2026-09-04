"""
Password hashing + JWT issuance/verification for real request-scoped
identity — replaces the previous "trust an X-User-Email header" stand-in
(see deps.py). Stateless (JWT, not a server-side sessions table): a login
verifies the password once and issues a signed token carrying the user's
id/email/role/tenant_id; every subsequent request verifies the signature
and expiry only, no DB round-trip required to authenticate a request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

# Blank counts as missing — a set-but-empty var must not become the key.
JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set — tokens cannot be signed or verified. "
        "deployment/sh/dev.sh generates one into deployment/.env; for the native "
        "path export it in scripts/start_local.sh (Knowledge and DID services must "
        "share the same value)."
    )
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)

Role = Literal["superadmin", "admin", "viewer"]


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    role: Role
    tenant_id: str | None  # None == superadmin, scoped to no single tenant


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user["role"],
        "tenant_id": str(user["tenant_id"]) if user["tenant_id"] is not None else None,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class InvalidTokenError(Exception):
    pass


def decode_access_token(token: str) -> CurrentUser:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    return CurrentUser(
        id=payload["sub"],
        email=payload["email"],
        role=payload["role"],
        tenant_id=payload["tenant_id"],
    )
