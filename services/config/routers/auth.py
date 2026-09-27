from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import google_oauth
from .. import users as users_service
from ..auth import CurrentUser, create_access_token
from ..deps import get_authenticated_user, is_platform_scoped
from ..google_oauth import GoogleOAuthError
from ..schemas import BootstrapRequest, ChangePasswordRequest, LoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])

_GOOGLE_COOKIE = "yuviz_google_oauth"
_GOOGLE_COOKIE_PATH = "/auth/oauth/google"


def _token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": users_service.to_public_dict(user),
    }


@router.get("/setup-status")
async def setup_status():
    return {"setup_required": not await users_service.superadmin_exists()}


@router.post("/bootstrap", status_code=201)
async def bootstrap(body: BootstrapRequest):
    try:
        user = await users_service.bootstrap_first_superadmin(
            email=body.email, password=body.password,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="that email is already registered")
    if user is None:
        raise HTTPException(status_code=409, detail="setup has already been completed")
    return _token_response(user)


@router.post("/login")
async def login(body: LoginRequest):
    user = await users_service.authenticate(body.email, body.password)
    if user is None:
        # Same 401 for "no such email" and "wrong password" — see
        # users.authenticate()'s docstring for why.
        raise HTTPException(status_code=401, detail="invalid email or password")
    return _token_response(user)


def _to_login(**fragment: str) -> RedirectResponse:
    # URL fragment, not query: the browser never sends it to any server or logs.
    return RedirectResponse(f"{google_oauth.ADMIN_UI_URL}/login#{urlencode(fragment)}", status_code=302)


async def _google_user(mode: str, email: str) -> dict[str, Any]:
    if mode == "create":
        try:
            # Random unusable password: this account signs in with Google
            # until the user sets one via change-password.
            user = await users_service.bootstrap_first_superadmin(
                email=email, password=secrets.token_urlsafe(32),
            )
        except asyncpg.UniqueViolationError:
            raise GoogleOAuthError("That email is already registered — sign in instead.")
        if user is None:
            raise GoogleOAuthError("Setup has already been completed — sign in instead.")
        return user
    user = await users_service.get_user_by_email(email)
    if user is None or user.get("is_service_account"):
        raise GoogleOAuthError(f"No account exists for {email}. Ask an administrator for an invite.")
    return user


@router.get("/oauth/google/start")
async def google_start(mode: google_oauth.Mode = "signin"):
    if not google_oauth.enabled():
        return _to_login(error="Google sign-in is not configured on this server.")
    url, nonce = google_oauth.authorization_url(mode)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        _GOOGLE_COOKIE, nonce, max_age=google_oauth.STATE_TTL_SECONDS, path=_GOOGLE_COOKIE_PATH,
        httponly=True, samesite="lax", secure=google_oauth.REDIRECT_URI.startswith("https://"),
    )
    return resp


@router.get("/oauth/google/callback")
async def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
):
    try:
        if error or not code or not state:
            raise GoogleOAuthError("Google sign-in was cancelled.")
        mode, nonce = google_oauth.read_state(state, request.cookies.get(_GOOGLE_COOKIE))
        email = await google_oauth.verified_email(code, nonce)
        user = await _google_user(mode, email)
    except GoogleOAuthError as exc:
        resp = _to_login(error=str(exc))
    else:
        resp = _to_login(token=create_access_token(user))
    resp.delete_cookie(_GOOGLE_COOKIE, path=_GOOGLE_COOKIE_PATH)
    return resp


@router.get("/me")
async def me(current_user: CurrentUser = Depends(get_authenticated_user)):
    user = await users_service.get_user_by_id(current_user.id)
    if user is None:
        # Token is validly signed but the user row is gone (deleted since
        # the token was issued) — same posture as an expired token: 401, not
        # a 404 that would leak whether the id ever existed.
        raise HTTPException(status_code=401, detail="user no longer exists")
    return users_service.to_public_dict(user)


@router.post("/change-password", status_code=204)
async def change_password(
    body: ChangePasswordRequest, current_user: CurrentUser = Depends(get_authenticated_user),
):
    # Always the caller's own password — there is no "change someone else's
    # password" endpoint. An admin resetting another user's credentials is a
    # different, not-yet-built operation (would need its own audit shape:
    # "admin X reset user Y's password" is a materially different event from
    # "user Y changed their own password").
    ok = await users_service.change_password(
        current_user.id,
        current_password=body.current_password,
        new_password=body.new_password,
        platform_scoped=is_platform_scoped(current_user),
    )
    if not ok:
        raise HTTPException(status_code=400, detail="current password is incorrect")
