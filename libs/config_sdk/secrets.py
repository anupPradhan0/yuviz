"""
Provider credentials entered in the Admin UI, encrypted at rest.

env:/k8s: point at a secret provisioned elsewhere, which makes adding
a provider an ops task. `enc:` carries the credential instead: Config Service
encrypts here before it reaches Postgres, Conversation Service decrypts at
provider-construction time (secret_resolver.py's EncryptedResolver).

Fernet rather than anything hand-rolled — authenticated and versioned, and
this is exactly the place not to be clever. Lives in config_sdk because both
planes need the identical encoding.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid

ENCRYPTED_PREFIX = "enc:"
TENANT_BOUND_PREFIX = "enc:t1."
_ENV_VAR = "SECRET_ENCRYPTION_KEY"

# What reencrypt_tenant_refs.py writes over a ciphertext it found under more
# than one tenant. It lives here, not in each service, because every reader of
# a credential column has to recognise it and there are four of them — a copy
# per service is how one of them gets missed (lesson 42). Deliberately not
# NULL: NULL reads as "no credential configured" and is passed through
# silently, whereas this has no scheme, so every resolver rejects it.
QUARANTINED = "quarantined"


def is_quarantined(ref: str | None) -> bool:
    return ref == QUARANTINED


class SecretEncryptionUnavailable(RuntimeError):
    """Raised rather than falling back to plaintext: a credential store that
    quietly stops encrypting is worse than one that refuses to start."""


class SecretTenantMismatch(ValueError):
    """A tenant-bound ref that does not open for this tenant: another tenant's
    ciphertext, or a tampered one. Deliberately indistinguishable."""


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
    if is_tenant_bound(ref):
        # Shared resolvers have no tenant to bind to, so they must refuse
        # rather than reach Fernet with a ref it was never meant to open.
        raise ValueError("tenant-bound secret reference needs decrypt_tenant_secret")
    token = ref[len(ENCRYPTED_PREFIX):].encode()
    try:
        return _fernet().decrypt(token).decode()
    except SecretEncryptionUnavailable:
        raise
    except Exception:
        # Wrong key, or a row from another install. Never echo the ciphertext.
        raise SecretEncryptionUnavailable(
            "stored API key could not be decrypted — SECRET_ENCRYPTION_KEY does not match "
            "the one it was saved with. Re-enter the key on the provider."
        ) from None


def is_tenant_bound(ref: str | None) -> bool:
    return bool(ref) and ref.startswith(TENANT_BOUND_PREFIX)


def _aesgcm():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = os.environ.get(_ENV_VAR, "").strip()
    if not key:
        raise SecretEncryptionUnavailable(f"{_ENV_VAR} is not set — tenant secrets cannot be sealed or opened.")
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except (binascii.Error, ValueError):
        raise SecretEncryptionUnavailable(f"{_ENV_VAR} is not valid urlsafe base64") from None
    # info separates this key from Fernet's use of the same key bytes.
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"yuviz/enc-t1/tenant-bound").derive(raw)
    return AESGCM(derived)


def _tenant_aad(tenant_id: "str | uuid.UUID") -> bytes:
    # Canonicalised: callers pass both str and asyncpg's UUID.
    return b"yuviz:enc:t1|tenant:" + str(uuid.UUID(str(tenant_id))).encode()


def encrypt_tenant_secret(tenant_id: "str | uuid.UUID", plaintext: str) -> str:
    """Seal `plaintext` so it opens only for `tenant_id` (AEAD associated data)."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    aad = _tenant_aad(tenant_id)
    nonce = os.urandom(12)
    sealed = nonce + _aesgcm().encrypt(nonce, plaintext.encode(), aad)
    return TENANT_BOUND_PREFIX + base64.urlsafe_b64encode(sealed).decode()


def decrypt_tenant_secret(tenant_id: "str | uuid.UUID", ref: str) -> str:
    if not is_tenant_bound(ref):
        raise ValueError("not a tenant-bound secret reference")
    aad = _tenant_aad(tenant_id)
    gcm = _aesgcm()
    try:
        sealed = base64.urlsafe_b64decode(ref[len(TENANT_BOUND_PREFIX):].encode())
        return gcm.decrypt(sealed[:12], sealed[12:], aad).decode()
    except Exception:
        raise SecretTenantMismatch("credential_ref_outside_tenant_namespace") from None
