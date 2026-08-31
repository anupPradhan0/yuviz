"""
Provider credentials entered in the Admin UI, encrypted at rest.

The other three secret schemes (env:/k8s:/vault:) point at a secret someone
put somewhere else. That is right for a deployment with a secret manager,
and wrong for the person who just wants to pick Gemini and paste their key:
it makes adding a provider an ops task — edit .env, restart the container —
for something the UI is already asking about.

`enc:` closes that gap. The Admin UI takes the real key, Config Service
encrypts it here before it ever reaches Postgres, and Conversation Service
decrypts it at provider-construction time (see services/conversation/
secret_resolver.py's EncryptedResolver). What lands in the database is
ciphertext, so a leaked row, backup or audit trail still yields nothing.

One platform-level key does the work — SECRET_ENCRYPTION_KEY, generated
once by deployment/sh/dev.sh alongside JWT_SECRET. That is one secret in
the environment for the whole install, instead of one per provider.

Deliberately Fernet from `cryptography` rather than anything hand-rolled:
it is authenticated (AES-CBC + HMAC), versioned, and the one-line API is
hard to hold wrong. Encrypting credentials is exactly the place not to be
clever.

Lives in config_sdk because both planes need the identical encoding — the
same reason the workflow graph model lives here.
"""

from __future__ import annotations

import os

ENCRYPTED_PREFIX = "enc:"
_ENV_VAR = "SECRET_ENCRYPTION_KEY"


class SecretEncryptionUnavailable(RuntimeError):
    """SECRET_ENCRYPTION_KEY is missing or malformed. Raised rather than
    silently falling back to storing plaintext — a credential store that
    quietly stops encrypting is worse than one that refuses to start."""


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get(_ENV_VAR, "").strip()
    if not key:
        raise SecretEncryptionUnavailable(
            f"{_ENV_VAR} is not set — API keys cannot be encrypted or read back. "
            "deployment/sh/dev.sh generates one; add it to deployment/.env and "
            "restart the config and conversation services."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # malformed / wrong length
        raise SecretEncryptionUnavailable(f"{_ENV_VAR} is not a valid Fernet key: {exc}") from None


def generate_key() -> str:
    """A fresh urlsafe-base64 32-byte key, for dev.sh and the docs."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def is_encrypted(ref: str | None) -> bool:
    return bool(ref) and ref.startswith(ENCRYPTED_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Returns the `enc:<token>` reference to store in api_key_ref."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    return ENCRYPTED_PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ref: str) -> str:
    if not is_encrypted(ref):
        raise ValueError(f"not an encrypted secret reference: {ref[:12]!r}…")
    token = ref[len(ENCRYPTED_PREFIX):].encode()
    try:
        return _fernet().decrypt(token).decode()
    except SecretEncryptionUnavailable:
        raise
    except Exception:
        # Wrong key, or a row encrypted by a different install. Never echo
        # the ciphertext into a log line.
        raise SecretEncryptionUnavailable(
            "stored API key could not be decrypted — SECRET_ENCRYPTION_KEY does not match "
            "the one it was saved with. Re-enter the key on the provider."
        ) from None
