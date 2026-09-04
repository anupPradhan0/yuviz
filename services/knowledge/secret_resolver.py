"""Re-exports the shared implementation — see libs/config_sdk/secret_resolver.py."""

from __future__ import annotations

from libs.config_sdk.secret_resolver import (
    CompositeSecretResolver,
    EncryptedResolver,
    EnvResolver,
    K8sFileResolver,
    SecretResolver,
)

__all__ = [
    "CompositeSecretResolver",
    "EncryptedResolver",
    "EnvResolver",
    "K8sFileResolver",
    "SecretResolver",
]
