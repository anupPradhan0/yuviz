"""Google OAuth2 / OpenID Connect sign-in (authorization-code flow).

Google only proves who the browser is. Sign-in maps a verified Google email
onto an existing user; "create" registers a new organization admin, never a
superadmin (see routers/auth.py).
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

import httpx
import jwt

from .auth import JWT_ALGORITHM, JWT_SECRET

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/auth/oauth/google/callback",
).strip()
ADMIN_UI_URL = os.environ.get("ADMIN_UI_URL", "http://localhost:3000").strip().rstrip("/")

STATE_TTL_SECONDS = 600
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
_jwks = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")

Mode = Literal["signin", "create"]


class GoogleOAuthError(Exception):
    """Message is shown to the user on the login page."""


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    given_name: str | None = None
    family_name: str | None = None


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def authorization_url(mode: Mode) -> tuple[str, str]:
    """Returns (Google consent URL, nonce). The nonce must also be set as a
    browser cookie so the callback can prove it's the same browser (login CSRF)."""
    nonce = secrets.token_urlsafe(32)
    state = jwt.encode(
        {
            "purpose": "google-oauth",
            "mode": mode,
            "nonce": nonce,
            "exp": int(datetime.now(timezone.utc).timestamp()) + STATE_TTL_SECONDS,
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{_AUTH_URL}?{urlencode(params)}", nonce


def read_state(state: str, cookie_nonce: str | None) -> tuple[Mode, str]:
    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise GoogleOAuthError("Google sign-in expired — please try again.") from exc
    nonce = payload.get("nonce")
    if (
        payload.get("purpose") != "google-oauth"
        or payload.get("mode") not in ("signin", "create")
        or not isinstance(nonce, str)
        or not cookie_nonce
        or not secrets.compare_digest(nonce, cookie_nonce)
    ):
        raise GoogleOAuthError("Google sign-in could not be verified — please try again.")
    return payload["mode"], nonce


async def verified_identity(code: str, nonce: str) -> GoogleIdentity:
    """Exchanges the code and returns the ID token's identity, only if Google
    signed it for this client and marked the address verified."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TOKEN_URL, data={
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            })
        if resp.status_code != 200:
            raise GoogleOAuthError("Google rejected the sign-in — please try again.")
        id_token = resp.json()["id_token"]
        key = await asyncio.to_thread(_jwks.get_signing_key_from_jwt, id_token)
        claims = jwt.decode(
            id_token, key.key, algorithms=["RS256"], audience=CLIENT_ID, issuer=_ISSUERS,
        )
    except GoogleOAuthError:
        raise
    except (httpx.HTTPError, KeyError, ValueError, jwt.PyJWTError) as exc:
        raise GoogleOAuthError("Google sign-in failed — please try again.") from exc

    if claims.get("nonce") != nonce:
        raise GoogleOAuthError("Google sign-in could not be verified — please try again.")
    email = claims.get("email")
    if not email or claims.get("email_verified") is not True:
        raise GoogleOAuthError("Your Google account has no verified email address.")
    return GoogleIdentity(email, claims.get("given_name"), claims.get("family_name"))
