"""
Invite lifecycle — thin HTTP wrapper over invites.py, same split as
routers/users.py over users.py (CURSOR.md: routers translate HTTP <-> the
sibling module and nothing else).

The admin routes (create/list/resend/revoke) require superadmin/admin, same
as routers/users.py's write routes. The accept routes are public by
construction — no `Depends(get_current_user)` or `Depends(get_authenticated_
user)` at all — so they bypass the console gate without needing an
exemption list, and the invite token travels only in the `X-Invite-Token`
header, never a path or query segment (see invites.py / design doc's "Token
placement").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .. import email
from .. import invites as invites_service
from .. import users as users_service
from ..auth import CurrentUser
from ..deps import require_role
from ..schemas import InviteAccept, InviteCreate

router = APIRouter(prefix="/invites", tags=["invites"])


def _client_host(request: Request) -> str:
    # request.client is None on some transports (e.g. a Unix domain socket
    # in front of this service) — falling through to request.client.host
    # unguarded is an AttributeError -> 500, and worse, skips the throttle
    # entirely on these public, unauthenticated routes. A fixed sentinel
    # key means such requests still share one (real) rate-limit bucket
    # instead of bypassing the limiter altogether.
    return request.client.host if request.client is not None else "unknown-client"


@router.post("", status_code=201)
async def create_invite(
    body: InviteCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # Probe cap first, outcome-blind: incremented before create_invite's own
    # users-table lookup runs, so a 409 this raises costs the actor exactly
    # what a 201 would (design doc's "Probe rate limit"). Mail-bomb cap is
    # peeked here too but only incremented after a real send below.
    request.app.state.invite_throttle.check_probe(current_user.id)
    request.app.state.invite_throttle.check_send_cap(current_user.id)

    row, raw_token = await invites_service.create_invite(
        email=body.email, role=body.role, tenant_id=body.tenant_id, team=body.team,
        actor=current_user,
    )
    request.app.state.invite_throttle.record_send(current_user.id)

    email_sent = True
    try:
        await email.send_invite_email(to_email=row["email"], raw_token=raw_token)
    except Exception:
        # Non-fatal (AC12): the invite row is already committed pending and
        # stays resendable — a broken SMTP config must not 500 the request.
        email_sent = False

    result = invites_service.to_public_dict(row)
    result["email_sent"] = email_sent
    return result


@router.get("")
async def list_invites(
    tenant_id: str | None = None,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # `?tenant_id=` is honored only for super_admin; for anyone else it's
    # forced to the JWT's own tenant_id, not merely validated (AC11, CURSOR.md).
    scoped_tenant_id = tenant_id if current_user.role == "superadmin" else current_user.tenant_id
    rows = await invites_service.list_invites(
        tenant_id=scoped_tenant_id, is_superadmin=(current_user.role == "superadmin"),
    )
    return [invites_service.to_public_dict(row) for row in rows]


@router.post("/{invite_id}/resend")
async def resend_invite(
    invite_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    request.app.state.invite_throttle.check_send_cap(current_user.id)

    row, raw_token = await invites_service.resend_invite(invite_id, actor=current_user)
    request.app.state.invite_throttle.record_send(current_user.id)

    email_sent = True
    try:
        await email.send_invite_email(to_email=row["email"], raw_token=raw_token)
    except Exception:
        email_sent = False

    result = invites_service.to_public_dict(row)
    result["email_sent"] = email_sent
    return result


@router.post("/{invite_id}/revoke")
async def revoke_invite(
    invite_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    row = await invites_service.revoke_invite(invite_id, actor=current_user)
    return invites_service.to_public_dict(row)


@router.get("/accept")
async def get_invite_to_accept(
    request: Request, x_invite_token: str | None = Header(default=None),
):
    # Public — no identity dependency at all, so this bypasses the console
    # gate by construction rather than needing an exemption.
    request.app.state.accept_throttle.check(_client_host(request))
    if not x_invite_token:
        raise HTTPException(status_code=404, detail="invite not found")
    return await invites_service.get_invite_for_accept(raw_token=x_invite_token)


@router.post("/accept")
async def accept_invite(
    body: InviteAccept, request: Request, x_invite_token: str | None = Header(default=None),
):
    request.app.state.accept_throttle.check(_client_host(request))
    if not x_invite_token:
        raise HTTPException(status_code=404, detail="invite not found")
    user = await invites_service.accept_invite(raw_token=x_invite_token, password=body.password)
    return users_service.to_public_dict(user)
