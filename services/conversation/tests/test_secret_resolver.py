from __future__ import annotations


import pytest

from ..secret_resolver import CompositeSecretResolver, EnvResolver, K8sFileResolver


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


class TestCompositeSecretResolver:
    async def test_dispatches_env(self, monkeypatch):
        monkeypatch.setenv("COMPOSITE_TEST_VAR", "value-123")
        resolver = CompositeSecretResolver()
        assert await resolver.resolve("env:COMPOSITE_TEST_VAR") == "value-123"

    async def test_dispatches_k8s(self, tmp_path):
        (tmp_path / "svc-key").write_text("k8s-value")
        resolver = CompositeSecretResolver(k8s_mount_root=str(tmp_path))
        assert await resolver.resolve("k8s:svc-key") == "k8s-value"

    async def test_vault_raises_not_implemented(self):
        resolver = CompositeSecretResolver()
        with pytest.raises(NotImplementedError):
            await resolver.resolve("vault:secret/voiceai/prod/openai")

    async def test_unknown_scheme_raises_value_error(self):
        resolver = CompositeSecretResolver()
        with pytest.raises(ValueError):
            await resolver.resolve("ftp:nonsense")
