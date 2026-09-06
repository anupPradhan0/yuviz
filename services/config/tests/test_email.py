"""
send_invite_email() — the SMTP send itself is a blocking stdlib call, so
these tests are about the two things review found missing: an explicit
socket timeout (rather than inheriting the global "block forever" default),
and running that blocking call off the event loop so a slow/unreachable
SMTP host doesn't stall every other in-flight request (including /health).
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
import time
from unittest.mock import MagicMock, patch

import pytest

from services.config import email


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "invites@example.com")
    monkeypatch.setenv("SMTP_FROM", "invites@example.com")
    monkeypatch.setenv("SMTP_PASSWORD_REF", "env:SMTP_PASSWORD")
    monkeypatch.setenv("SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("INVITE_BASE_URL", "http://localhost:3000")


async def test_smtp_connection_is_made_with_an_explicit_timeout():
    # Would fail if the timeout kwarg were dropped again (the exact
    # regression review caught: smtplib.SMTP() with no timeout inherits
    # the global default of None, i.e. blocks forever).
    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = MagicMock()
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    args, kwargs = mock_smtp.call_args
    assert kwargs.get("timeout") == 10 or (len(args) >= 3 and args[2] == 10)


async def test_send_invite_email_does_not_block_the_event_loop():
    # A slow SMTP call, run correctly off-thread, must not delay a
    # concurrently-scheduled coroutine. Would fail (i.e. the "fast" marker
    # would land *after* SMTP finishes) if send_invite_email went back to
    # calling smtplib.SMTP directly on the event loop.
    order: list[str] = []

    def _slow_smtp(*args, **kwargs):
        time.sleep(0.3)
        order.append("smtp")
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock()
        cm.__exit__.return_value = False
        return cm

    async def _fast_task():
        await asyncio.sleep(0.05)
        order.append("fast")

    with patch("smtplib.SMTP", side_effect=_slow_smtp):
        await asyncio.gather(
            email.send_invite_email(to_email="new-user@example.com", raw_token="tok"),
            _fast_task(),
        )

    assert order == ["fast", "smtp"]


async def test_smtp_failure_raises_and_does_not_hang(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "203.0.113.1")  # TEST-NET-3, guaranteed unroutable
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")


async def test_starttls_is_called_by_default():
    # SMTP_STARTTLS defaults to true — both SMTP_PASSWORD and the invite
    # token are bearer-equivalent, so this must not be an opt-in. Would
    # fail if starttls() were dropped (the exact regression this fix is
    # for: credentials and tokens going out in cleartext on :587).
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.starttls.assert_called_once()
    # starttls must happen before login, or the credential still goes out
    # in cleartext even though the call was made.
    call_names = [call_obj[0] for call_obj in smtp_instance.method_calls]
    assert call_names.index("starttls") < call_names.index("login")


async def test_starttls_uses_a_verifying_tls_context():
    # starttls() with no context= falls back to ssl._create_stdlib_context(),
    # which is CERT_NONE/check_hostname=False — encrypted but unverified, so
    # an active MITM can still present any certificate and capture the SMTP
    # password and the invite token. Would fail if the context= argument
    # were dropped, or replaced with anything less strict than
    # ssl.create_default_context()'s CERT_REQUIRED/check_hostname=True.
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    _, kwargs = smtp_instance.starttls.call_args
    context = kwargs.get("context")
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_smtp_starttls_false_skips_it(monkeypatch):
    # The documented escape hatch for a local dev relay with no TLS at all
    # (MailHog, aiosmtpd on :1025) — only an explicit "false" disables it.
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.starttls.assert_not_called()
    smtp_instance.login.assert_called_once()


async def test_login_is_called_when_smtp_user_is_set():
    # The default fixture env has SMTP_USER set — this is the baseline the
    # "skipped when unset" test below is contrasted against.
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_called_once_with("invites@example.com", "not-a-real-password")


async def test_login_is_skipped_when_smtp_user_is_unset(monkeypatch):
    # A relay that requires no authentication (a local dev sink, or an
    # internal relay that authorises by source IP) doesn't advertise AUTH —
    # calling login() unconditionally would fail every such send with
    # SMTPNotSupportedError. Presence of SMTP_USER is the only signal, no
    # separate toggle.
    monkeypatch.delenv("SMTP_USER", raising=False)
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_called_once()


async def test_unresolvable_password_ref_fails_loudly_when_smtp_user_is_set(monkeypatch):
    # SMTP_USER being set must make the credential mandatory — a broken
    # SMTP_PASSWORD_REF must never be silently treated the same as "no
    # SMTP_USER configured" and fall through to an unauthenticated send.
    monkeypatch.setenv("SMTP_PASSWORD_REF", "env:SMTP_PASSWORD_DOES_NOT_EXIST")
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        with pytest.raises(KeyError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_not_called()


async def test_relay_refusing_starttls_is_a_clean_send_failure_not_a_cleartext_send():
    # A relay that doesn't support STARTTLS must fail the send, never fall
    # back to sending the password/token in cleartext. Would fail (i.e.
    # login/send_message would have been called) if starttls()'s exception
    # were caught and swallowed anywhere in the send path.
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        smtp_instance.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS extension not supported")
        mock_smtp.return_value.__enter__.return_value = smtp_instance

        with pytest.raises(smtplib.SMTPNotSupportedError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_not_called()
