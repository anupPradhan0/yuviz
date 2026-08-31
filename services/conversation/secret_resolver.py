"""
SecretResolver — turns a provider_configs.api_key_ref (or carriers.auth_token_ref)
string into the actual secret value, exactly once, at provider-instantiation
time — never per-call (see AIProviderManager and
project_phase5_schema_design.md's non-negotiable latency rule #4).

Ref formats:
  "env:VAR_NAME"                 — EnvResolver:    reads an environment variable
  "k8s:namespace/secret"         — K8sFileResolver: reads a Kubernetes-mounted secret file
  "vault:kv-path#field"          — VaultResolver:   reads one field of a KV v2 secret

The Admin UI only ever stores/displays the ref string itself, never a
resolved value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import hvac


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


class VaultResolver:
    """
    ref = 'vault:kv-path#field' -> reads one field of a HashiCorp Vault KV v2
    secret, e.g. 'vault:voiceai/providers/groq#api_key' reads the 'api_key'
    field of the secret at '{mount_point}/data/voiceai/providers/groq'.

    Address and auth token come from VAULT_ADDR/VAULT_TOKEN env vars — a
    static token is fine for the local dev instance this was built against;
    production should switch to AppRole auth (role_id/secret_id) without
    changing this class's resolve() contract, only how the client below is
    constructed.
    """

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


class EncryptedResolver:
    """ref = 'enc:<fernet-token>' -> the key an operator pasted into the Admin
    UI. The other schemes point at a secret; this one carries it, sealed."""

    async def resolve(self, ref: str) -> str:
        from libs.config_sdk.secrets import decrypt_secret

        return decrypt_secret(ref)


class CompositeSecretResolver:
    """Dispatches by ref prefix to the resolver that owns that scheme."""

    def __init__(self, k8s_mount_root: str = "/var/run/secrets", vault_mount_point: str = "secret") -> None:
        self._env = EnvResolver()
        self._k8s = K8sFileResolver(k8s_mount_root)
        self._vault = VaultResolver(vault_mount_point)
        self._enc = EncryptedResolver()

    async def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            return await self._env.resolve(ref)
        if ref.startswith("k8s:"):
            return await self._k8s.resolve(ref)
        if ref.startswith("vault:"):
            return await self._vault.resolve(ref)
        if ref.startswith("enc:"):
            return await self._enc.resolve(ref)
        # Never log `ref` here — the likeliest cause is a raw API key pasted
        # into the reference field, and echoing it leaks the key.
        raise ValueError(
            f"unrecognized secret ref scheme (expected env:/k8s:/vault:/enc:), got {ref.split(':')[0][:12]!r}"
        )
