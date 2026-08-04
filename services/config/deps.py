"""
Shared FastAPI dependencies — real request-scoped identity.

get_current_user() verifies a signed JWT (see auth.py) and returns the
CurrentUser it encodes; it raises 401 itself rather than returning None, so
a route depending on it is unreachable without a valid token — there is no
"forgot to check" failure mode the way an optional dependency would have.
This replaces the previous current_user_email, which only read a
caller-supplied header with no verification at all — real, but never
enforced, identity.

require_role() builds on top of it for endpoints that need more than "any
authenticated user" (e.g. only superadmin/admin may write, viewer is
read-only).
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException, Header

from .auth import CurrentUser, InvalidTokenError, decode_access_token


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or expired token")


def require_role(*allowed_roles: str):
    """Returns a dependency that additionally rejects (403) an authenticated
    user whose role isn't in allowed_roles. Usage:
        Depends(require_role("superadmin", "admin"))
    """

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot perform this action")
        return user

    return _check


async def get_or_404(fetch: Awaitable[Any | None], detail: str) -> Any:
    """Await `fetch`, raising a clean 404 with `detail` if it resolves to
    None, instead of letting the caller repeat the same if-None-raise
    block. Same fetch-then-check shape used to be inlined separately in
    each router (tenants, agents, calls, phone_numbers, provider_configs)."""
    row = await fetch
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


async def validate_id_exists(
    id_: str | None,
    fetch_by_id: Callable[[str], Awaitable[Any | None]],
    entity_name: str,
) -> None:
    """UUID-format-then-existence check for a foreign-key id in a request
    body, e.g. phone_numbers.agent_id — a clean 400 (malformed id) or 404
    (well-formed but nonexistent) instead of an INSERT's FK violation
    reaching the client as a raw 500. A None id_ (optional FK, e.g. no
    fallback_agent_id given) is a no-op, not an error."""
    if id_ is None:
        return
    try:
        uuid.UUID(id_)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{id_!r} is not a valid {entity_name} id")
    if await fetch_by_id(id_) is None:
        raise HTTPException(status_code=404, detail=f"{entity_name} {id_!r} not found")
