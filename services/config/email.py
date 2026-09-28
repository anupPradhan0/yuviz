"""
Invite and verification-code email delivery — stdlib smtplib, no new dependency.

Raises on any config/SMTP failure; callers decide whether that is fatal.
SMTP_PASSWORD_REF is a secret ref (env:/k8s:/enc:), like provider credentials.
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

# Own pool, not asyncio.to_thread's: a stuck SMTP thread can't be cancelled,
# and on the shared pool enough of them would starve bcrypt and block login.
_SMTP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smtp")


def close_smtp_executor() -> None:
    """Lifespan teardown. wait=False so a thread stuck in smtplib can't block
    shutdown (or leak a pool per `uvicorn --reload`)."""
    _SMTP_EXECUTOR.shutdown(wait=False)

# Per socket operation; _send also caps the whole send with wait_for.
_SMTP_TIMEOUT_SECONDS = 10


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set — cannot send email. See docs/setup.md.")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("false", "0", "no")


def _send_sync(
    *, host: str, port: int, user: str | None, password: str | None, message: EmailMessage, starttls: bool,
) -> None:
    """Blocking I/O — run on _SMTP_EXECUTOR only. A failed STARTTLS raises
    (no cleartext fallback); the default SSL context verifies the certificate."""
    with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


async def send_invite_email(*, to_email: str, raw_token: str) -> None:
    """The token rides in the URL fragment, so it never reaches a server log
    or Referer header."""
    base_url = _env("INVITE_BASE_URL").rstrip("/")
    link = f"{base_url}/invite#{raw_token}"
    await _send(
        to_email=to_email,
        subject="You've been invited to Voice AI Platform",
        body=(
            "You've been invited to set up an account.\n\n"
            f"Follow this link to accept and set your password: {link}\n\n"
            "This link expires in 7 days."
        ),
    )


async def send_verification_code_email(*, to_email: str, code: str, minutes_valid: int) -> None:
    await _send(
        to_email=to_email,
        subject=f"{code} is your Yuviz verification code",
        body=(
            f"Your verification code is: {code}\n\n"
            f"It expires in {minutes_valid} minutes. If you didn't request this, ignore this email."
        ),
    )


async def _send(*, to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT"))
    user = os.environ.get("SMTP_USER", "").strip()
    from_addr = _env("SMTP_FROM")
    starttls = _env_bool("SMTP_STARTTLS", default=True)
    # No SMTP_USER = relay needs no auth; if set, an unresolvable password must fail loudly.
    password = await _resolver.resolve(_env("SMTP_PASSWORD_REF")) if user else None

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_email
    message.set_content(body)

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
