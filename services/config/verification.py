"""
Emailed 6-digit codes for public signup, signed-in email change and forgot
password. No user row exists until a signup verifies; errors raise after
commit so wrong-guess counters persist.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from libs.tenancy import platform_conn

from . import audit, auth, db, users

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_SENDS_PER_HOUR = 5

_WRONG_CODE = "That code is incorrect."
_NOT_PENDING = "No verification is pending for this email — please start again."


class EmailTaken(Exception):
    pass


class ResendTooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"try again in {retry_after}s")
        self.retry_after = retry_after


class CodeError(Exception):
    """Message is shown to the user."""


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash(purpose: str, subject: str, code: str) -> str:
    msg = f"{purpose}:{subject}:{code}".encode()
    return hmac.new(auth.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code_problem(row: Any, purpose: str, subject: str, code: str) -> str | None:
    if row is None:
        return _NOT_PENDING
    if row["attempts"] >= MAX_ATTEMPTS:
        return "Too many incorrect attempts — request a new code."
    if row["expires_at"] <= _now():
        return "That code has expired — request a new one."
    if not hmac.compare_digest(row["code_hash"], _hash(purpose, subject, code)):
        return _WRONG_CODE
    return None


def _next_send(row: Any, now: datetime) -> tuple[datetime, int]:
    """(send_window_start, send_count) for one more send to this pending
    signup; raises ResendTooSoon inside the cooldown or over the hourly cap."""
    if row is None:
        return now, 1
    wait = RESEND_COOLDOWN_SECONDS - (now - row["last_sent_at"]).total_seconds()
    if wait > 0:
        raise ResendTooSoon(math.ceil(wait))
    window_end = row["send_window_start"] + timedelta(hours=1)
    if now >= window_end:
        return now, 1
    if row["send_count"] >= MAX_SENDS_PER_HOUR:
        raise ResendTooSoon(math.ceil((window_end - now).total_seconds()))
    return row["send_window_start"], row["send_count"] + 1


async def _email_in_use(conn: Any, email: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE lower(email) = lower($1) AND deleted_at IS NULL)", email,
    )


# ── Public signup ────────────────────────────────────────────────────────────

async def start_registration(
    *,
    email: str,
    password: str,
    organization_name: str,
    first_name: str,
    last_name: str,
    phone: str,
    signup_source: str,
) -> str:
    """Stores (or replaces) the pending signup and returns the code to email.
    Raises EmailTaken or ResendTooSoon; writes nothing in either case."""
    email = email.lower()
    password_hash = await asyncio.to_thread(auth.hash_password, password)
    code = _new_code()
    now = _now()
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        if await _email_in_use(conn, email):
            raise EmailTaken()
        existing = await conn.fetchrow(
            "SELECT * FROM pending_registrations WHERE email = $1 FOR UPDATE", email,
        )
        window_start, send_count = _next_send(existing, now)
        await conn.execute(
            "INSERT INTO pending_registrations (email, password_hash, organization_name, first_name, "
            "last_name, phone, signup_source, code_hash, expires_at, attempts, last_sent_at, "
            "send_window_start, send_count) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, $10, $11, $12) "
            "ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, "
            "organization_name = EXCLUDED.organization_name, first_name = EXCLUDED.first_name, "
            "last_name = EXCLUDED.last_name, phone = EXCLUDED.phone, "
            "signup_source = EXCLUDED.signup_source, code_hash = EXCLUDED.code_hash, "
            "expires_at = EXCLUDED.expires_at, attempts = 0, last_sent_at = EXCLUDED.last_sent_at, "
            "send_window_start = EXCLUDED.send_window_start, send_count = EXCLUDED.send_count",
            email, password_hash, organization_name, first_name, last_name, phone, signup_source,
            _hash("register", email, code), now + timedelta(minutes=CODE_TTL_MINUTES),
            now, window_start, send_count,
        )
    return code


async def discard_registration(email: str) -> None:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        await conn.execute("DELETE FROM pending_registrations WHERE email = $1", email.lower())


async def resend_registration_code(email: str) -> str | None:
    """New code for a pending signup, or None if there is none. Raises
    ResendTooSoon."""
    email = email.lower()
    code = _new_code()
    now = _now()
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        row = await conn.fetchrow("SELECT * FROM pending_registrations WHERE email = $1 FOR UPDATE", email)
        if row is None:
            return None
        window_start, send_count = _next_send(row, now)
        await conn.execute(
            "UPDATE pending_registrations SET code_hash = $2, expires_at = $3, attempts = 0, "
            "last_sent_at = $4, send_window_start = $5, send_count = $6 WHERE email = $1",
            email, _hash("register", email, code), now + timedelta(minutes=CODE_TTL_MINUTES),
            now, window_start, send_count,
        )
    return code


async def verify_registration(email: str, code: str) -> dict[str, Any]:
    """Creates the organization + admin from the pending signup and returns
    the user row. Raises CodeError or EmailTaken."""
    email = email.lower()
    user = None
    taken = False
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        row = await conn.fetchrow("SELECT * FROM pending_registrations WHERE email = $1 FOR UPDATE", email)
        problem = _code_problem(row, "register", email, code)
        if problem == _WRONG_CODE:
            await conn.execute(
                "UPDATE pending_registrations SET attempts = attempts + 1 WHERE email = $1", email,
            )
        elif problem is None:
            await conn.execute("DELETE FROM pending_registrations WHERE email = $1", email)
            if await _email_in_use(conn, email):
                taken = True
            else:
                user = await users.register_admin_on(
                    conn,
                    email=email,
                    password_hash=row["password_hash"],
                    organization_name=row["organization_name"],
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    phone=row["phone"],
                    signup_source=row["signup_source"],
                )
    if problem is not None:
        raise CodeError(problem)
    if taken:
        raise EmailTaken()
    return user


async def pending_password_matches(email: str, password: str) -> bool:
    """True if `email` is an unverified signup and `password` is the one it
    was registered with — lets login say "verify first" without leaking
    whether an address has a pending signup to someone without the password."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-login") as conn:
        password_hash = await conn.fetchval(
            "SELECT password_hash FROM pending_registrations WHERE email = $1", email.lower(),
        )
    if password_hash is None:
        return False
    return await asyncio.to_thread(auth.verify_password, password, password_hash)


# ── Signed-in email change ───────────────────────────────────────────────────

async def request_email_change(
    *, user_id: Any, tenant_id: Any, current_password: str, new_email: str,
) -> str | None:
    """Opens (or replaces) the caller's change request and returns the code to
    email to `new_email`. None if the password is wrong. Raises EmailTaken or
    ResendTooSoon."""
    new_email = new_email.lower()
    code = _new_code()
    now = _now()
    pool = await db.get_pool()
    async with platform_conn(pool, reason="users-email-change", stamp_tenant=tenant_id) as conn:
        user = await conn.fetchrow(
            "SELECT password_hash, password_set FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
        )
        if user is None:
            raise LookupError(f"user {user_id} not found")
        if user["password_set"] and not await asyncio.to_thread(
            auth.verify_password, current_password, user["password_hash"],
        ):
            return None
        if await _email_in_use(conn, new_email):
            raise EmailTaken()
        last_sent = await conn.fetchval(
            "SELECT last_sent_at FROM email_change_requests WHERE user_id = $1 FOR UPDATE", user_id,
        )
        if last_sent is not None:
            wait = RESEND_COOLDOWN_SECONDS - (now - last_sent).total_seconds()
            if wait > 0:
                raise ResendTooSoon(math.ceil(wait))
        await conn.execute(
            "INSERT INTO email_change_requests (user_id, new_email, code_hash, expires_at, attempts, last_sent_at) "
            "VALUES ($1, $2, $3, $4, 0, $5) "
            "ON CONFLICT (user_id) DO UPDATE SET new_email = EXCLUDED.new_email, "
            "code_hash = EXCLUDED.code_hash, expires_at = EXCLUDED.expires_at, attempts = 0, "
            "last_sent_at = EXCLUDED.last_sent_at",
            user_id, new_email, _hash("email-change", f"{user_id}:{new_email}", code),
            now + timedelta(minutes=CODE_TTL_MINUTES), now,
        )
    return code


async def discard_email_change(*, user_id: Any, tenant_id: Any) -> None:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="users-email-change", stamp_tenant=tenant_id) as conn:
        await conn.execute("DELETE FROM email_change_requests WHERE user_id = $1", user_id)


async def confirm_email_change(*, user_id: Any, tenant_id: Any, code: str) -> dict[str, Any]:
    """Switches the caller's email to the requested address and returns the
    updated user row. Raises CodeError."""
    new = None
    pool = await db.get_pool()
    async with platform_conn(pool, reason="users-email-change", stamp_tenant=tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM email_change_requests WHERE user_id = $1 FOR UPDATE", user_id)
        subject = f"{user_id}:{row['new_email']}" if row is not None else ""
        problem = _code_problem(row, "email-change", subject, code)
        if problem == _WRONG_CODE:
            await conn.execute(
                "UPDATE email_change_requests SET attempts = attempts + 1 WHERE user_id = $1", user_id,
            )
        elif problem is None:
            await conn.execute("DELETE FROM email_change_requests WHERE user_id = $1", user_id)
            if await _email_in_use(conn, row["new_email"]):
                problem = "That email is already registered."
            else:
                old_email = await conn.fetchval("SELECT email FROM users WHERE id = $1 FOR UPDATE", user_id)
                new = dict(await conn.fetchrow(
                    "UPDATE users SET email = $2, updated_at = now() WHERE id = $1 RETURNING *",
                    user_id, row["new_email"],
                ))
                await audit.write_audit(
                    conn,
                    entity_type="user",
                    entity_id=user_id,
                    action="updated",
                    user_id=user_id,
                    user_email=old_email,
                    old_value={"email": old_email},
                    new_value={"email": new["email"]},
                )
    if problem is not None:
        raise CodeError(problem)
    return new


# ── Forgot password ──────────────────────────────────────────────────────────

async def _reset_account(email: str) -> dict[str, Any] | None:
    user = await users.get_user_by_email(email)
    return None if user is None or user["is_service_account"] else user


async def start_password_reset(email: str) -> str | None:
    """Opens (or replaces) the account's reset request and returns the code to
    email, or None if no account has this address. Raises ResendTooSoon."""
    user = await _reset_account(email)
    if user is None:
        return None
    code = _new_code()
    now = _now()
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-password-reset") as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM password_reset_requests WHERE user_id = $1 FOR UPDATE", user["id"],
        )
        window_start, send_count = _next_send(existing, now)
        await conn.execute(
            "INSERT INTO password_reset_requests (user_id, code_hash, expires_at, attempts, last_sent_at, "
            "send_window_start, send_count) VALUES ($1, $2, $3, 0, $4, $5, $6) "
            "ON CONFLICT (user_id) DO UPDATE SET code_hash = EXCLUDED.code_hash, "
            "expires_at = EXCLUDED.expires_at, attempts = 0, last_sent_at = EXCLUDED.last_sent_at, "
            "send_window_start = EXCLUDED.send_window_start, send_count = EXCLUDED.send_count",
            user["id"], _hash("password-reset", str(user["id"]), code),
            now + timedelta(minutes=CODE_TTL_MINUTES), now, window_start, send_count,
        )
    return code


async def discard_password_reset(email: str) -> None:
    user = await _reset_account(email)
    if user is None:
        return
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-password-reset") as conn:
        await conn.execute("DELETE FROM password_reset_requests WHERE user_id = $1", user["id"])


async def reset_password(email: str, code: str, new_password: str) -> dict[str, Any]:
    """Sets a new password from a valid reset code and returns the user row.
    Raises CodeError."""
    user = await _reset_account(email)
    if user is None:
        raise CodeError(_NOT_PENDING)
    user_id = user["id"]
    updated = None
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-password-reset", stamp_tenant=user["tenant_id"]) as conn:
        row = await conn.fetchrow("SELECT * FROM password_reset_requests WHERE user_id = $1 FOR UPDATE", user_id)
        problem = _code_problem(row, "password-reset", str(user_id), code)
        if problem == _WRONG_CODE:
            await conn.execute(
                "UPDATE password_reset_requests SET attempts = attempts + 1 WHERE user_id = $1", user_id,
            )
        elif problem is None:
            await conn.execute("DELETE FROM password_reset_requests WHERE user_id = $1", user_id)
            new_hash = await asyncio.to_thread(auth.hash_password, new_password)
            updated = dict(await conn.fetchrow(
                "UPDATE users SET password_hash = $2, password_set = true, updated_at = now() "
                "WHERE id = $1 RETURNING *",
                user_id, new_hash,
            ))
            await audit.write_audit(
                conn,
                entity_type="user",
                entity_id=user_id,
                action="updated",
                user_id=user_id,
                user_email=user["email"],
                old_value={"password_hash": user["password_hash"]},
                new_value={"password_hash": new_hash},
            )
    if problem is not None:
        raise CodeError(problem)
    return updated
