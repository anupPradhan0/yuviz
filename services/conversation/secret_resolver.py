"""
SecretResolver — turns a provider_configs.api_key_ref (or carriers.auth_token_ref)
string into the actual secret value, exactly once, at provider-instantiation
time — never per-call (see AIProviderManager and
project_phase5_schema_design.md's non-negotiable latency rule #4).

Ref formats:
  "env:VAR_NAME"           — EnvResolver:    reads an environment variable
  "k8s:namespace/secret"   — K8sFileResolver: reads a Kubernetes-mounted secret file
  "vault:..."              — Phase 7, not implemented — raises NotImplementedError

The Admin UI only ever stores/displays the ref string itself, never a
resolved value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class SecretResolver(Protocol):
    async def resolve(self, ref: str) -> str:
        """Returns the resolved secret value. Raises if the ref cannot be resolved."""
        ...


class EnvResolver:
    """ref = 'env:VAR_NAME' -> os.environ['VAR_NAME']."""

    async def resolve(self, ref: str) -> str:
        key = ref.removeprefix("env:")
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"EnvResolver: environment variable {key!r} is not set (ref={ref!r})")
        return value


class K8sFileResolver:
    """
    ref = 'k8s:namespace/secret-name' -> reads the file a Kubernetes Secret
    volume mount would place at {mount_root}/{namespace}/{secret-name}.

    Kubernetes mounts each key of a Secret as its own file under the mount
    path; provider_configs stores one api_key_ref per provider, so the
    convention here is one file per secret, named by the ref's last segment.
    """

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


class CompositeSecretResolver:
    """Dispatches by ref prefix to the resolver that owns that scheme."""

    def __init__(self, k8s_mount_root: str = "/var/run/secrets") -> None:
        self._env = EnvResolver()
        self._k8s = K8sFileResolver(k8s_mount_root)

    async def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            return await self._env.resolve(ref)
        if ref.startswith("k8s:"):
            return await self._k8s.resolve(ref)
        if ref.startswith("vault:"):
            raise NotImplementedError(
                f"vault: secret refs are Phase 7 — got ref={ref!r}"
            )
        raise ValueError(f"unrecognized secret ref scheme (expected env:/k8s:/vault:): {ref!r}")
