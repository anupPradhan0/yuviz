"""TOOLEXEC_INTERNAL_ENDPOINTS: an exact host:port may reach a private address, nothing else."""

from __future__ import annotations

import pytest

from services.toolexec import custom_apis


@pytest.fixture
def clinic_listed(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_INTERNAL_ENDPOINTS", "localhost:8700")
    monkeypatch.setenv("TOOLEXEC_HTTP_HOST_ALLOWLIST", "localhost")

    async def _resolver(hostname, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


@pytest.mark.asyncio
async def test_listed_endpoint_may_resolve_to_loopback(clinic_listed):
    host, ips = await custom_apis.resolve_and_validate_endpoint("http://localhost:8700/tool/v1/availability")
    assert (host, ips) == ("localhost", ["127.0.0.1"])


@pytest.mark.asyncio
async def test_other_port_on_listed_host_still_denied(clinic_listed):
    with pytest.raises(ValueError, match="denied address"):
        await custom_apis.resolve_and_validate_endpoint("http://localhost:8092/auth/login")


@pytest.mark.asyncio
async def test_listed_endpoint_never_reaches_link_local(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_INTERNAL_ENDPOINTS", "metadata.internal:80")
    monkeypatch.setenv("TOOLEXEC_HTTP_HOST_ALLOWLIST", "metadata.internal")

    async def _resolver(hostname, port):
        return ["169.254.169.254"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    with pytest.raises(ValueError, match="denied address"):
        await custom_apis.resolve_and_validate_endpoint("http://metadata.internal/latest/meta-data/")


@pytest.mark.asyncio
async def test_unlisted_still_denied(monkeypatch):
    monkeypatch.delenv("TOOLEXEC_INTERNAL_ENDPOINTS", raising=False)
    monkeypatch.setenv("TOOLEXEC_HTTP_HOST_ALLOWLIST", "localhost")

    async def _resolver(hostname, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    with pytest.raises(ValueError, match="denied address"):
        await custom_apis.resolve_and_validate_endpoint("http://localhost:8700/tool/v1/availability")
