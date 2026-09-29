"""Unit tests for google_oauth.read_state / verified_identity — no database, no
network. Google's token endpoint is an httpx MockTransport and ID tokens are
signed with a throwaway RSA key standing in for Google's JWKS."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from services.config import google_oauth
from services.config.google_oauth import GoogleOAuthError

CLIENT_ID = "test-client.apps.googleusercontent.com"
NONCE = "the-nonce"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RealAsyncClient = httpx.AsyncClient


def _id_token(key=_KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "admin@example.com",
        "email_verified": True,
        "nonce": NONCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture(autouse=True)
def google():
    """Configures the client and routes token-endpoint calls to `google.respond`."""
    state = SimpleNamespace(respond=lambda request: httpx.Response(200, json={"id_token": _id_token()}))
    transport = httpx.MockTransport(lambda request: state.respond(request))
    with patch.object(google_oauth, "CLIENT_ID", CLIENT_ID), \
         patch.object(google_oauth, "CLIENT_SECRET", "test-secret"), \
         patch.object(google_oauth.httpx, "AsyncClient",
                      lambda **kw: _RealAsyncClient(transport=transport, **kw)), \
         patch.object(google_oauth._jwks, "get_signing_key_from_jwt",  # noqa: SLF001
                      lambda token: SimpleNamespace(key=_KEY.public_key())):
        yield state


def _returns(token: str):
    return lambda request: httpx.Response(200, json={"id_token": token})


class TestVerifiedIdentity:
    async def test_valid_token_returns_identity_and_sends_the_code(self, google):
        seen = {}

        def respond(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"id_token": _id_token(given_name="Ada", family_name="Lovelace")})

        google.respond = respond
        identity = await google_oauth.verified_identity("auth-code", NONCE)
        assert identity == google_oauth.GoogleIdentity("admin@example.com", "Ada", "Lovelace")
        assert "code=auth-code" in seen["body"]
        assert "grant_type=authorization_code" in seen["body"]

    async def test_legacy_issuer_without_scheme_is_accepted(self, google):
        google.respond = _returns(_id_token(iss="accounts.google.com"))
        assert (await google_oauth.verified_identity("c", NONCE)).email == "admin@example.com"

    @pytest.mark.parametrize(
        "token",
        [
            _id_token(nonce="someone-elses-nonce"),
            _id_token(nonce=None),
            _id_token(email_verified=False),
            _id_token(email_verified="true"),
            _id_token(email_verified=None),
            _id_token(email=None),
            _id_token(aud="another-client.apps.googleusercontent.com"),
            _id_token(iss="https://evil.example.com"),
            _id_token(exp=int(time.time()) - 60),
            _id_token(key=_OTHER_KEY),
        ],
        ids=[
            "nonce-mismatch", "nonce-missing", "email-unverified", "email-verified-as-string",
            "email-verified-missing", "email-missing", "wrong-audience", "wrong-issuer",
            "expired", "bad-signature",
        ],
    )
    async def test_rejects_untrustworthy_id_tokens(self, google, token):
        google.respond = _returns(token)
        with pytest.raises(GoogleOAuthError):
            await google_oauth.verified_identity("c", NONCE)

    async def test_hs256_token_is_rejected(self, google):
        # Algorithm confusion: only RS256 may be accepted.
        google.respond = _returns(
            jwt.encode({"aud": CLIENT_ID}, "a-shared-secret-at-least-32-bytes-long", algorithm="HS256"),
        )
        with pytest.raises(GoogleOAuthError):
            await google_oauth.verified_identity("c", NONCE)

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(400, json={"error": "invalid_grant"}),
            httpx.Response(200, json={}),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"id_token": "not-a-jwt"}),
        ],
        ids=["token-endpoint-400", "no-id-token", "non-json", "malformed-id-token"],
    )
    async def test_bad_token_endpoint_responses_raise_user_facing_error(self, google, response):
        google.respond = lambda request: response
        with pytest.raises(GoogleOAuthError):
            await google_oauth.verified_identity("c", NONCE)

    async def test_network_failure_raises_user_facing_error(self, google):
        def respond(request):
            raise httpx.ConnectError("unreachable", request=request)

        google.respond = respond
        with pytest.raises(GoogleOAuthError):
            await google_oauth.verified_identity("c", NONCE)


class TestReadState:
    def test_round_trips_mode_and_nonce_from_authorization_url(self):
        url, nonce = google_oauth.authorization_url("create")
        state = httpx.URL(url).params["state"]
        assert google_oauth.read_state(state, nonce) == ("create", nonce)

    def test_nonce_cookie_mismatch_is_rejected(self):
        url, _ = google_oauth.authorization_url("signin")
        state = httpx.URL(url).params["state"]
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(state, "a-different-nonce")

    def test_missing_cookie_is_rejected(self):
        url, nonce = google_oauth.authorization_url("signin")
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(httpx.URL(url).params["state"], None)

    def test_expired_state_is_rejected(self):
        with patch.object(google_oauth, "STATE_TTL_SECONDS", -1):
            url, nonce = google_oauth.authorization_url("signin")
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(httpx.URL(url).params["state"], nonce)

    @pytest.mark.parametrize(
        "claims",
        [
            {"purpose": "something-else", "mode": "signin", "nonce": NONCE},
            {"purpose": "google-oauth", "mode": "superadmin", "nonce": NONCE},
            {"purpose": "google-oauth", "mode": "signin"},
        ],
        ids=["wrong-purpose", "unknown-mode", "no-nonce"],
    )
    def test_malformed_state_claims_are_rejected(self, claims):
        state = jwt.encode(
            {**claims, "exp": int(time.time()) + 60}, google_oauth.JWT_SECRET, algorithm="HS256",
        )
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(state, NONCE)

    def test_state_signed_with_another_key_is_rejected(self):
        state = jwt.encode(
            {"purpose": "google-oauth", "mode": "create", "nonce": NONCE, "exp": int(time.time()) + 60},
            "attacker-key-that-is-long-enough-for-hs256-xx", algorithm="HS256",
        )
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(state, NONCE)

    def test_an_access_token_cannot_be_replayed_as_state(self):
        # Both are HS256 under JWT_SECRET; `purpose` is what keeps them apart.
        token = google_oauth.jwt.encode(
            {"sub": "u", "nonce": NONCE, "mode": "create", "exp": int(time.time()) + 60},
            google_oauth.JWT_SECRET, algorithm="HS256",
        )
        with pytest.raises(GoogleOAuthError):
            google_oauth.read_state(token, NONCE)
