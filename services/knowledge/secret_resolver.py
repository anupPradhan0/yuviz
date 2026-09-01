"""
SecretResolver for Knowledge Service's embedding providers — same ref
format (env:/k8s:/enc:) and same "resolve once, at instantiation time"
contract as services/conversation/secret_resolver.py. Duplicated rather
than cross-imported: a small, self-contained utility, and Knowledge Service
should not depend on services.conversation internals any more than it
should depend on services.config internals for configuration reads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class SecretResolver(Protocol):
    async def resolve(self, ref: str) -> str: ...


class EnvResolver:
    async def resolve(self, ref: str) -> str:
        key = ref.removeprefix("env:")
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"EnvResolver: environment variable {key!r} is not set (ref={ref!r})")
        return value


class K8sFileResolver:
    def __init__(self, mount_root: str = "/var/run/secrets") -> None:
        self._mount_root = Path(mount_root)

    async def resolve(self, ref: str) -> str:
        path_part = ref.removeprefix("k8s:")
        secret_path = self._mount_root / path_part
        try:
            return secret_path.read_text().strip()
        except FileNotFoundError as exc:
            raise KeyError(f"K8sFileResolver: no secret file at {secret_path} (ref={ref!r})") from exc


class EncryptedResolver:
    """ref = 'enc:<fernet-token>' -> the key an operator pasted into the Admin
    UI. This one carries the credential sealed inside it."""

    async def resolve(self, ref: str) -> str:
        from libs.config_sdk.secrets import decrypt_secret

        return decrypt_secret(ref)


class CompositeSecretResolver:
    def __init__(self, k8s_mount_root: str = "/var/run/secrets") -> None:
        self._env = EnvResolver()
        self._k8s = K8sFileResolver(k8s_mount_root)
        self._enc = EncryptedResolver()

    async def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            return await self._env.resolve(ref)
        if ref.startswith("k8s:"):
            return await self._k8s.resolve(ref)
        if ref.startswith("enc:"):
            return await self._enc.resolve(ref)
        # Never log `ref` here — the likeliest cause is a raw API key pasted
        # into the reference field, and echoing it leaks the key.
        raise ValueError(
            f"unrecognized secret ref scheme (expected env:/k8s:/enc:), "
            f"got {ref.split(':')[0][:12]!r}"
        )
