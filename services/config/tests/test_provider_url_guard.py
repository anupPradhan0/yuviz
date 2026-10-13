"""Issue #97: an embedding config's base_url is vetted when it is written, with a 400 that says what to change."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.egress_guard import ALLOWED_HOSTS_ENV
from services.config.app import app


@pytest_asyncio.fixture(loop_scope="session")
async def admin(test_admin):
    headers = {"Authorization": f"Bearer {test_admin['token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c


def _body(base_url, role="embedding"):
    return {"name": "Embed", "role": role, "engine": "ollama", "model": "nomic-embed-text", "extra": {"base_url": base_url}}


@pytest.mark.parametrize("base_url", [
    "http://ollama.example.com:11434/api/pull#", "http://ollama.example.com:11434?x=", "http://169.254.169.254/",
    "http://10.0.0.5:11434", "http://127.0.0.1:6379", "http://2130706433", "http://[::ffff:10.0.0.1]",
    "ftp://ollama.example.com", "http://user:pw@ollama.example.com",
])
async def test_an_unsafe_embedding_base_url_is_a_clear_400_and_nothing_is_stored(admin, pool, test_tenant, base_url):
    resp = await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body(base_url))
    assert resp.status_code == 400
    assert "extra.base_url rejected" in resp.json()["detail"]
    assert await pool.fetchval("SELECT count(*) FROM provider_configs WHERE tenant_id = $1", test_tenant["id"]) == 0


async def test_a_public_origin_and_the_default_dev_endpoint_are_accepted(admin, test_tenant):
    for url in ("https://ollama.example.com", "http://localhost:11434", "http://127.0.0.1:11434"):
        resp = await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body(url))
        assert resp.status_code == 201, (url, resp.text)


async def test_the_operator_allow_list_is_honoured_at_write_time(admin, test_tenant, monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "10.0.0.5:11434")
    assert (await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body("http://10.0.0.5:11434"))).status_code == 201
    assert (await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body("http://10.0.0.5:6379"))).status_code == 400


async def test_the_update_path_is_guarded_too(admin, test_tenant):
    created = await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body("https://ollama.example.com"))
    resp = await admin.patch(
        f"/providers/{created.json()['id']}", json={"extra": {"base_url": "http://ollama.example.com:11434/api/pull#"}})
    assert resp.status_code == 400 and "extra.base_url rejected" in resp.json()["detail"]
    ok = await admin.patch(f"/providers/{created.json()['id']}", json={"extra": {"base_url": "https://other.example.com"}})
    assert ok.status_code == 200


async def test_other_roles_are_not_affected(admin, test_tenant):
    """Out of scope here (listed in the issue as follow-ups): only role 'embedding' is checked."""
    resp = await admin.post(f"/tenants/{test_tenant['id']}/providers", json=_body("http://10.0.0.5:11434", role="llm"))
    assert resp.status_code == 201
