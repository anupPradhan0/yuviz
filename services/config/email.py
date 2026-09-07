"""
Invite email delivery — stdlib smtplib, no new dependency.

Send is inline and non-fatal to the caller (see routers/invites.py): the
invite row is already committed pending by the time this is called, so a
failure here must not undo it or fail the request — the router catches
whatever this raises, reports `email_sent: false` (AC12), and the row
stays pending/resendable. This module raises on any config/SMTP failure;
it does not swallow anything itself.

SMTP_PASSWORD_REF is resolved through the same secret-ref convention every
provider/carrier credential already uses (libs/config_sdk/secret_resolver.py)
rather than a bare SMTP_PASSWORD env var, so an operator can point it at
env:/k8s: without this feature inventing a second scheme. The other SMTP_*
values are plain config, not secrets, so they're read directly from the
environment. See docs/setup.md for the full list of new env vars.
"""

from __future__ import annotations

import asyncio
import functools
import os
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

from .secret_resolver import CompositeSecretResolver, SecretResolver

_resolver: SecretResolver = CompositeSecretResolver()

# A dedicated (small, bounded) pool for the blocking SMTP call below, not
# asyncio.to_thread's default executor — that default pool (min(32, cpu+4)
# workers) is shared with every other blocking call this process threads,
# including auth.hash_password/verify_password (users.py). asyncio.wait_for
# below bounds how long *this coroutine* waits, but it cannot cancel the
# worker thread smtplib is actually blocked in — a black-holing relay can
# leave that thread stuck in a socket call for up to 4x
# _SMTP_TIMEOUT_SECONDS (one per round-trip) after the coroutine has already
# given up and returned. On the shared default pool enough stuck sends
# exhaust every worker and POST /auth/login queues behind them; on this
# pool a stuck send can only starve other invite sends, never login.
_SMTP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="invite-smtp")


def close_smtp_executor() -> None:
    """Called from app.py's lifespan teardown, same place db.close_pool()/
    cache.close() run. concurrent.futures registers its own atexit hook
    that joins every non-daemon executor thread it has ever created, so
    without an explicit shutdown() here that hook — not this module — is
    what eventually reaps _SMTP_EXECUTOR's threads, and only at interpreter
    exit: a SIGTERM during a black-holed send would otherwise block
    process exit for up to ~40s (well past docker-compose's grace period,
    forcing a SIGKILL), and `uvicorn --reload` leaks a fresh 4-thread pool
    every reload since nothing ever shuts the old one down. wait=False: a
    thread already stuck inside a blocking smtplib call can't be
    interrupted by shutting the pool down around it — that limitation is
    inherent to any thread-based worker, not something this call fixes —
    so this stops issuing new work and lets the process proceed with
    shutdown instead of blocking teardown on threads already stuck."""
    _SMTP_EXECUTOR.shutdown(wait=False)

# Design-specified ceiling on the blocking SMTP call below — without it,
# smtplib.SMTP() inherits the global socket default (no timeout at all), so
# an unreachable/black-holing host would hang forever. This is a *per-socket-
# operation* timeout, though: connect, starttls(), login() and send_message()
# are each a separate round-trip smtplib.SMTP() applies it to individually,
# so a relay that black-holes on each step in turn could burn up to 4x this
# before _send_sync raises. send_invite_email wraps the whole call in
# asyncio.wait_for() with this same value so the request is bounded by one
# 10s ceiling regardless of how many socket ops stall.
_SMTP_TIMEOUT_SECONDS = 10


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set — cannot send invite email. See docs/setup.md.")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("false", "0", "no")


def _send_sync(
    *, host: str, port: int, user: str | None, password: str | None, message: EmailMessage, starttls: bool,
) -> None:
    """The actual blocking socket I/O — run off the event loop via
    asyncio.to_thread (same pattern services/conversation's TTS/STT
    providers use for their own blocking calls, e.g. kokoro.py's
    loop.run_in_executor). Never call this directly from async code.

    STARTTLS happens here, inside the same blocking call, not as a
    separate awaited step — it's another blocking socket round-trip, and
    splitting it out would put it back on the event loop (lesson 18).
    smtplib.starttls() raises (SMTPNotSupportedError/SMTPException) if the
    relay doesn't support or refuses it — that propagates as-is, never
    caught here, so a relay that can't do STARTTLS is a send failure, not a
    silent cleartext fallback.

    ssl.create_default_context() is passed explicitly — starttls() with no
    context= falls back to ssl._create_stdlib_context(), which is
    CERT_NONE/check_hostname=False. That defeats passive eavesdropping but
    not an active MITM presenting any certificate. There is deliberately no
    knob to turn verification off while leaving encryption on: SMTP_STARTTLS
    already covers the one legitimate no-TLS case (a local dev relay), and a
    second, subtler "encrypted but unverified" mode is exactly how a
    cleartext-equivalent config ends up in production.

    login() is skipped entirely when `user` is empty — a relay that
    requires no authentication (a local dev sink, or an internal relay that
    authorises by source IP) doesn't advertise AUTH at all, and calling
    login() unconditionally fails every such send with SMTPNotSupportedError.
    The presence of SMTP_USER is the only signal; there is no separate
    toggle (send_invite_email is what decides `user`, by resolving the
    password only when SMTP_USER is actually set, so this never becomes a
    silent way to skip auth someone meant to configure)."""
    with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


async def send_invite_email(*, to_email: str, raw_token: str) -> None:
    """Composes and sends the invite link. The token rides in the URL
    *fragment* (`#<token>`), never a path segment — fragments never reach a
    server, so the raw token can't land in an access log or Referer header
    (see design doc's "Token placement"). Raises on any config or SMTP
    failure.

    STARTTLS is on by default (SMTP_STARTTLS) — the invite token and the
    SMTP_PASSWORD_REF credential are both bearer-equivalent, so sending
    either in cleartext over port 587 defeats the point of a secret-ref.
    The only legitimate reason to set SMTP_STARTTLS=false is a local dev
    relay (MailHog, aiosmtpd on :1025) that offers no TLS at all — this
    must stay true everywhere else.

    SMTP_USER is optional: an unset/empty value means the relay needs no
    authentication (a local dev sink, or an internal relay that authorises
    by source IP), and the send skips login() entirely — see _send_sync.
    But if SMTP_USER *is* set, the password is mandatory and resolved
    eagerly right here: a set-but-unresolvable SMTP_PASSWORD_REF must fail
    the send loudly, never silently fall through to an unauthenticated
    one just because the credential lookup broke.

    The blocking call is wrapped in asyncio.wait_for(..., _SMTP_TIMEOUT_
    SECONDS) — _send_sync's own timeout= is per socket operation, so
    without this outer bound a relay that black-holes on connect, then
    starttls(), then login(), then send_message() in turn could take up to
    4x _SMTP_TIMEOUT_SECONDS before raising. This is what actually gives
    the caller's non-fatal except Exception (routers/invites.py) its
    bounded time. It runs on _SMTP_EXECUTOR, not asyncio.to_thread's
    default pool — see that constant's comment for why."""
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT"))
    user = os.environ.get("SMTP_USER", "").strip()
    from_addr = _env("SMTP_FROM")
    base_url = _env("INVITE_BASE_URL").rstrip("/")
    starttls = _env_bool("SMTP_STARTTLS", default=True)
    password = await _resolver.resolve(_env("SMTP_PASSWORD_REF")) if user else None

    link = f"{base_url}/invite#{raw_token}"
    message = EmailMessage()
    message["Subject"] = "You've been invited to Voice AI Platform"
    message["From"] = from_addr
    message["To"] = to_email
    message.set_content(
        "You've been invited to set up an account.\n\n"
        f"Follow this link to accept and set your password: {link}\n\n"
        "This link expires in 7 days.",
    )

    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(
            _SMTP_EXECUTOR,
            functools.partial(
                _send_sync, host=host, port=port, user=user, password=password, message=message, starttls=starttls,
            ),
        ),
        timeout=_SMTP_TIMEOUT_SECONDS,
    )
