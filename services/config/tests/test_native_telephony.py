"""Native (local SIP) telephony configs: the platform's shared Kamailio/
FreeSWITCH. Only a superadmin creates them or places numbers under them;
one per tenant; tenant admins can still route their native numbers."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import cache
from services.config.app import app


def _client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def native_config(test_tenant, test_superadmin, pool):
    async with _client(test_superadmin["token"]) as su:
        resp = await su.post(f"/tenants/{test_tenant['id']}/telephony-configs", json={"name": "Native", "provider": "native"})
    assert resp.status_code == 201, resp.text
    yield resp.json()
    await pool.execute("UPDATE phone_numbers SET telephony_config_id = NULL WHERE telephony_config_id = $1", resp.json()["id"])
    await pool.execute("DELETE FROM telephony_configs WHERE id = $1", resp.json()["id"])


@pytest.fixture
def did():
    return f"test-did-{uuid.uuid4().hex[:8]}"


async def test_discovery_lists_native_with_no_credential_fields(test_superadmin):
    async with _client(test_superadmin["token"]) as su:
        resp = await su.get("/telephony-providers")
    assert resp.json()["native"] == {"required": [], "sensitive": []}


async def test_superadmin_creates_one_native_config_per_tenant(test_tenant, test_superadmin, native_config):
    assert native_config["provider"] == "native" and native_config["credentials"] == {}
    async with _client(test_superadmin["token"]) as su:
        second = await su.post(f"/tenants/{test_tenant['id']}/telephony-configs", json={"name": "Native 2", "provider": "native"})
    assert second.status_code == 409


async def test_native_config_can_never_be_default_outbound(test_tenant, test_superadmin, native_config, pool):
    async with _client(test_superadmin["token"]) as su:
        set_default = await su.post(f"/telephony-configs/{native_config['id']}/set-default-outbound")
        patched = await su.patch(f"/telephony-configs/{native_config['id']}", json={"is_default_outbound": True})
    assert (set_default.status_code, patched.status_code) == (400, 400)
    await pool.execute("DELETE FROM telephony_configs WHERE id = $1", native_config["id"])
    async with _client(test_superadmin["token"]) as su:
        created = await su.post(
            f"/tenants/{test_tenant['id']}/telephony-configs",
            json={"name": "Native", "provider": "native", "is_default_outbound": True},
        )
    assert created.status_code == 400


async def test_tenant_admin_cannot_create_or_change_a_native_config(test_tenant, test_admin, native_config):
    async with _client(test_admin["token"]) as admin:
        created = await admin.post(f"/tenants/{test_tenant['id']}/telephony-configs", json={"name": "Mine", "provider": "native"})
        patched = await admin.patch(f"/telephony-configs/{native_config['id']}", json={"name": "Renamed"})
        deleted = await admin.delete(f"/telephony-configs/{native_config['id']}")
    assert (created.status_code, patched.status_code, deleted.status_code) == (403, 403, 403)


async def test_superadmin_adds_a_native_number_and_it_routes(test_tenant, test_superadmin, native_config, did, pool):
    try:
        async with _client(test_superadmin["token"]) as su:
            resp = await su.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": native_config["id"]},
            )
        assert resp.status_code == 201, resp.text
        assert (await cache.get_json(f"did:{did}"))["tenant_slug"] == test_tenant["slug"]
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)
        await cache.invalidate(f"did:{did}")


async def test_tenant_admin_cannot_add_a_native_or_providerless_number(test_tenant, test_admin, native_config, did):
    async with _client(test_admin["token"]) as admin:
        native = await admin.post(
            f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": native_config["id"]},
        )
        bare = await admin.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})
    assert (native.status_code, bare.status_code) == (403, 403)


async def test_superadmin_attaches_an_existing_number_to_native(test_tenant, test_superadmin, native_config, did, pool):
    try:
        async with _client(test_superadmin["token"]) as su:
            created = await su.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})
            attached = await su.patch(
                f"/phone-numbers/{created.json()['id']}", json={"telephony_config_id": native_config["id"]},
            )
        assert attached.status_code == 200, attached.text
        assert attached.json()["telephony_config_id"] == native_config["id"]
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)
        await cache.invalidate(f"did:{did}")


async def test_tenant_admin_can_route_but_not_move_a_native_number(
    test_tenant, test_superadmin, test_admin, native_config, did, pool,
):
    try:
        async with _client(test_superadmin["token"]) as su:
            created = await su.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": native_config["id"]},
            )
        number_id = created.json()["id"]
        async with _client(test_admin["token"]) as admin:
            status_change = await admin.patch(f"/phone-numbers/{number_id}", json={"status": "inactive"})
            did_change = await admin.patch(f"/phone-numbers/{number_id}", json={"did": f"{did}-x"})
            detach = await admin.patch(f"/phone-numbers/{number_id}", json={"telephony_config_id": None})
        assert (status_change.status_code, did_change.status_code, detach.status_code) == (200, 403, 403)
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did LIKE $1", f"{did}%")
        await cache.invalidate(f"did:{did}")
