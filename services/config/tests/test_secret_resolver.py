from __future__ import annotations

import hvac.exceptions
import pytest
import requests

from ..secret_resolver import CompositeSecretResolver, EnvResolver, K8sFileResolver, VaultResolver


class TestEnvResolver:
    async def test_resolves_existing_var(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_SECRET", "sk-abc123")
        resolver = EnvResolver()
        assert await resolver.resolve("env:MY_TEST_SECRET") == "sk-abc123"

    async def test_missing_var_raises_key_error(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_NOT_SET_VAR", raising=False)
        resolver = EnvResolver()
        with pytest.raises(KeyError):
            await resolver.resolve("env:DEFINITELY_NOT_SET_VAR")


class TestK8sFileResolver:
    async def test_resolves_mounted_secret_file(self, tmp_path):
        secret_dir = tmp_path / "voiceai"
        secret_dir.mkdir()
        (secret_dir / "deepgram-api-key").write_text("dg-secret-value\n")

        resolver = K8sFileResolver(mount_root=str(tmp_path))
        value = await resolver.resolve("k8s:voiceai/deepgram-api-key")
        assert value == "dg-secret-value"  # trailing newline stripped

    async def test_missing_file_raises_key_error(self, tmp_path):
        resolver = K8sFileResolver(mount_root=str(tmp_path))
        with pytest.raises(KeyError):
            await resolver.resolve("k8s:voiceai/does-not-exist")


class TestVaultResolver:
    """VaultResolver.resolve() previously only caught hvac.exceptions.
    InvalidPath — a bad/expired VAULT_TOKEN (Forbidden), a sealed Vault
    (VaultDown), or an unreachable host (requests.ConnectionError) all
    reached the caller as an unhandled exception (a bare 500 with no
    detail at the HTTP layer). Confirmed live once already this session:
    a process running without VAULT_TOKEN hit exactly the Forbidden case
    and silently fell back to legacy config elsewhere in the stack."""

    def _resolver_with_fake_read(self, fake_read):
        resolver = VaultResolver()
        resolver._client.secrets.kv.v2.read_secret_version = fake_read
        return resolver

    async def test_malformed_ref_raises_value_error(self):
        resolver = VaultResolver()
        with pytest.raises(ValueError):
            await resolver.resolve("vault:no-hash-field")

    async def test_invalid_path_raises_key_error(self):
        def fake_read(**kwargs):
            raise hvac.exceptions.InvalidPath("no secret here")

        resolver = self._resolver_with_fake_read(fake_read)
        with pytest.raises(KeyError):
            await resolver.resolve("vault:voiceai/nope#api_key")

    async def test_forbidden_raises_value_error_not_unhandled(self):
        def fake_read(**kwargs):
            raise hvac.exceptions.Forbidden("permission denied")

        resolver = self._resolver_with_fake_read(fake_read)
        with pytest.raises(ValueError, match="permission denied|VAULT_TOKEN"):
            await resolver.resolve("vault:voiceai/providers/elevenlabs#api_key")

    async def test_vault_down_raises_value_error_not_unhandled(self):
        def fake_read(**kwargs):
            raise hvac.exceptions.VaultDown("vault is sealed")

        resolver = self._resolver_with_fake_read(fake_read)
        with pytest.raises(ValueError):
            await resolver.resolve("vault:voiceai/providers/elevenlabs#api_key")

    async def test_connection_error_raises_value_error_not_unhandled(self):
        def fake_read(**kwargs):
            raise requests.exceptions.ConnectionError("could not connect")

        resolver = self._resolver_with_fake_read(fake_read)
        with pytest.raises(ValueError):
            await resolver.resolve("vault:voiceai/providers/elevenlabs#api_key")

    async def test_field_missing_from_secret_raises_key_error(self):
        def fake_read(**kwargs):
            return {"data": {"data": {"other_field": "x"}}}

        resolver = self._resolver_with_fake_read(fake_read)
        with pytest.raises(KeyError):
            await resolver.resolve("vault:voiceai/providers/elevenlabs#api_key")


class TestCompositeSecretResolver:
    async def test_dispatches_env(self, monkeypatch):
        monkeypatch.setenv("COMPOSITE_TEST_VAR", "value-123")
        resolver = CompositeSecretResolver()
        assert await resolver.resolve("env:COMPOSITE_TEST_VAR") == "value-123"

    async def test_dispatches_k8s(self, tmp_path):
        (tmp_path / "svc-key").write_text("k8s-value")
        resolver = CompositeSecretResolver(k8s_mount_root=str(tmp_path))
        assert await resolver.resolve("k8s:svc-key") == "k8s-value"

    async def test_unknown_scheme_raises_value_error(self):
        resolver = CompositeSecretResolver()
        with pytest.raises(ValueError):
            await resolver.resolve("ftp:nonsense")
