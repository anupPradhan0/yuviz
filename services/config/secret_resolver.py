"""
SecretResolver for Config Service — same ref format (env:/k8s:/vault:) and
same "resolve once, per use" contract as services/conversation/
secret_resolver.py. Duplicated rather than cross-imported: a small,
self-contained utility, and Config Service should not depend on
services.conversation internals any more than Knowledge Service should
depend on services.conversation internals for its own copy (see
services/knowledge/secret_resolver.py) — the "don't extract libs/auth_sdk
yet" instruction's spirit applies equally here: a little duplication now,
no premature shared library.

Used to call a third-party provider's own API server-side on the admin-ui's
behalf (e.g. listing an ElevenLabs account's voices) — the resolved secret
value is used for that one outbound call and never returned to the caller.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import hvac


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
            raise KeyError(
                f"K8sFileResolver: no secret file at {secret_path} (ref={ref!r})"
            ) from exc


class VaultResolver:
    def __init__(self, mount_point: str = "secret") -> None:
        self._mount_point = mount_point
        self._client = hvac.Client(
            url=os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200"),
            token=os.environ.get("VAULT_TOKEN"),
        )

    async def resolve(self, ref: str) -> str:
        path_and_field = ref.removeprefix("vault:")
        if "#" not in path_and_field:
            raise ValueError(
                f"VaultResolver: ref must be 'vault:path#field' (ref={ref!r})"
            )
        path, field = path_and_field.rsplit("#", 1)
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount_point, raise_on_deleted_version=True,
            )
        except hvac.exceptions.InvalidPath as exc:
            raise KeyError(f"VaultResolver: no secret at {path!r} (ref={ref!r})") from exc
        data = response["data"]["data"]
        if field not in data:
            raise KeyError(f"VaultResolver: field {field!r} not found at {path!r} (ref={ref!r})")
        return data[field]


class CompositeSecretResolver:
    """Dispatches by ref prefix to the resolver that owns that scheme."""

    def __init__(self, k8s_mount_root: str = "/var/run/secrets", vault_mount_point: str = "secret") -> None:
        self._env = EnvResolver()
        self._k8s = K8sFileResolver(k8s_mount_root)
        self._vault = VaultResolver(vault_mount_point)

    async def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            return await self._env.resolve(ref)
        if ref.startswith("k8s:"):
            return await self._k8s.resolve(ref)
        if ref.startswith("vault:"):
            return await self._vault.resolve(ref)
        raise ValueError(f"unrecognized secret ref scheme (expected env:/k8s:/vault:): {ref!r}")
