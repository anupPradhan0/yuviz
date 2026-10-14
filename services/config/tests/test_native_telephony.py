"""Native (local SIP) telephony configs: the platform's shared Kamailio/
FreeSWITCH. Only a superadmin creates them or places numbers under them;
one per tenant; tenant admins can still route their native numbers."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import agents, cache
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
    return f"9{uuid.uuid4().int % 10**10:010d}"


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
            did_change = await admin.patch(f"/phone-numbers/{number_id}", json={"did": f"{did}9"})
            detach = await admin.patch(f"/phone-numbers/{number_id}", json={"telephony_config_id": None})
        assert (status_change.status_code, did_change.status_code, detach.status_code) == (200, 403, 403)

        async with _client(test_admin["token"]) as admin:
            admin_delete = await admin.delete(f"/phone-numbers/{number_id}")
        assert admin_delete.status_code == 403
        assert await pool.fetchval("SELECT deleted_at FROM phone_numbers WHERE id = $1", number_id) is None
        async with _client(test_superadmin["token"]) as su:
            su_delete = await su.delete(f"/phone-numbers/{number_id}")
        assert su_delete.status_code == 204
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did LIKE $1", f"{did}%")
        await cache.invalidate(f"did:{did}")


async def test_tenant_admin_cannot_claim_a_local_extension_under_any_provider(test_tenant, test_admin, pool):
    """Routing keys on the DID alone, so a REST config or carrier must not
    let a tenant admin take a local extension."""
    ext = f"59{uuid.uuid4().int % 10**4:04d}"  # extensions are at most 6 digits
    vobiz = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Fake', 'fake', '{}'::jsonb) RETURNING id", test_tenant["id"],
    )
    carrier = await pool.fetchval(
        "INSERT INTO carriers (tenant_id, name, provider) VALUES ($1, 'Twilio', 'twilio') RETURNING id", test_tenant["id"],
    )
    public = f"+1555{uuid.uuid4().int % 10**7:07d}"
    try:
        async with _client(test_admin["token"]) as admin:
            via_config = await admin.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": ext, "telephony_config_id": str(vobiz)},
            )
            via_carrier = await admin.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": ext, "carrier_id": str(carrier)},
            )
            created = await admin.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": public, "carrier_id": str(carrier)},
            )
            renamed = await admin.patch(f"/phone-numbers/{created.json()['id']}", json={"did": ext})
        assert (via_config.status_code, via_carrier.status_code) == (403, 403)
        assert created.status_code == 201, created.text
        assert renamed.status_code == 403
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", vobiz)
        await pool.execute("DELETE FROM carriers WHERE id = $1", carrier)
        await cache.invalidate(f"did:{public}")


async def test_tenant_admin_can_add_a_public_number_without_a_plus(test_tenant, test_admin, pool):
    """Vobiz routes its numbers as E.164 without the "+"."""
    config = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Fake', 'fake', '{}'::jsonb) RETURNING id", test_tenant["id"],
    )
    did = f"91{uuid.uuid4().int % 10**10:010d}"
    try:
        async with _client(test_admin["token"]) as admin:
            resp = await admin.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": str(config)},
            )
        assert resp.status_code == 201, resp.text
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config)
        await cache.invalidate(f"did:{did}")


async def test_a_public_number_on_a_deleted_rest_config_stays_the_tenants(test_tenant, test_admin, test_superadmin, pool):
    config = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Fake', 'fake', '{}'::jsonb) RETURNING id", test_tenant["id"],
    )
    did = f"+1555{uuid.uuid4().int % 10**7:07d}"
    try:
        async with _client(test_admin["token"]) as admin:
            created = await admin.post(
                f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": str(config)},
            )
            assert created.status_code == 201, created.text
            await pool.execute("UPDATE telephony_configs SET deleted_at = now() WHERE id = $1", config)
            await cache.invalidate(f"telephony_config:{config}")
            resp = await admin.delete(f"/phone-numbers/{created.json()['id']}")
        assert resp.status_code == 204, resp.text
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config)
        await cache.invalidate(f"did:{did}")


async def test_a_number_whose_config_was_deleted_stays_platform_only(
    test_tenant, test_superadmin, test_admin, native_config, did, pool,
):
    try:
        async with _client(test_superadmin["token"]) as su:
            created = await su.post(
                f"/tenants/{test_tenant['id']}/phone-numbers",
                json={"did": f"+1555{uuid.uuid4().int % 10**7:07d}", "telephony_config_id": native_config["id"]},
            )
        assert created.status_code == 201, created.text
        await pool.execute("UPDATE telephony_configs SET deleted_at = now() WHERE id = $1", native_config["id"])
        await cache.invalidate(f"telephony_config:{native_config['id']}")
        async with _client(test_admin["token"]) as admin:
            resp = await admin.delete(f"/phone-numbers/{created.json()['id']}")
        assert resp.status_code == 403
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])


async def test_re_adding_a_did_replaces_the_previous_owners_cached_route(test_tenant, test_superadmin, did, pool):
    await cache.set_json(f"did:{did}", {"tenant_slug": "previous-owner", "agent_slug": "x", "version": None})
    try:
        async with _client(test_superadmin["token"]) as su:
            created = await su.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})
        assert created.status_code == 201, created.text
        cached = await cache.get_json(f"did:{did}")
        assert cached["tenant_slug"] == test_tenant["slug"]
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)
        await cache.invalidate(f"did:{did}")


async def test_tenant_admin_cannot_reach_a_local_extension_by_reformatting_it(test_tenant, test_admin, pool, scoped):
    """A SIP URI holding an extension must stay its own address, never the extension's route.
    The four phone-shaped forms are shape-guard coverage: they canonicalise to the extension."""
    ext = f"59{uuid.uuid4().int % 10**4:04d}"
    agent = await agents.create_agent(tenant_id=test_tenant["id"], slug="takeover", name="Takeover")
    carrier = await pool.fetchval(
        "INSERT INTO carriers (tenant_id, name, provider) VALUES ($1, 'Twilio', 'twilio') RETURNING id", test_tenant["id"],
    )
    try:
        async with _client(test_admin["token"]) as admin:
            disguised = [
                await admin.post(f"/tenants/{test_tenant['id']}/phone-numbers",
                                 json={"did": form, "carrier_id": str(carrier)})
                for form in (f"+{ext}", f"{ext[:3]} {ext[3:]}", f"({ext[:3]}) {ext[3:]}", f"{ext[:3]}-{ext[3:]}")
            ]
            sip = await admin.post(f"/tenants/{test_tenant['id']}/phone-numbers",
                                   json={"did": f"sip:{ext}@attacker.example", "carrier_id": str(carrier),
                                         "agent_id": str(agent["id"])})
        assert [r.status_code for r in disguised] == [403] * 4
        # A SIP URI is its own address: stored as typed, never the extension's route.
        assert sip.status_code == 201, sip.text
        assert sip.json()["did"] == f"sip:{ext}@attacker.example"
        assert await cache.get_json(f"did:{ext}") is None
        assert (await cache.get_json(f"did:sip:{ext}@attacker.example"))["agent_slug"] == "takeover"
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
        await pool.execute("DELETE FROM carriers WHERE id = $1", carrier)
        await cache.invalidate(f"did:sip:{ext}@attacker.example")


async def test_retyping_a_number_in_another_format_is_not_a_move(test_tenant, test_admin, pool, monkeypatch):
    """A move detaches the number from the provider; a format-only edit must not."""
    from services.config import number_sync

    detached: list[str] = []

    async def record_detach(config, did):
        detached.append(did)
        return {"ok": True, "message": None}

    monkeypatch.setattr(number_sync, "detach", record_detach)
    config = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Fake', 'fake', '{}'::jsonb) RETURNING id", test_tenant["id"],
    )
    digits = f"1555{uuid.uuid4().int % 10**7:07d}"
    try:
        async with _client(test_admin["token"]) as admin:
            created = await admin.post(f"/tenants/{test_tenant['id']}/phone-numbers",
                                       json={"did": digits, "telephony_config_id": str(config)})
            assert created.status_code == 201, created.text
            retyped = await admin.patch(f"/phone-numbers/{created.json()['id']}",
                                        json={"did": f"+{digits[0]} ({digits[1:4]}) {digits[4:]}"})
        assert retyped.status_code == 200, retyped.text
        assert retyped.json()["did"] == digits
        assert detached == []
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config)
        await cache.invalidate(f"did:{digits}")


@pytest.mark.parametrize("did", ["tel:+918065354620", "+91 80\u20136535 4620", "+91\u200980 6535 4620", "ext101"])
async def test_a_did_that_could_never_route_is_rejected(test_tenant, test_superadmin, did):
    async with _client(test_superadmin["token"]) as su:
        resp = await su.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})
    assert resp.status_code == 422, resp.text


async def test_patching_did_to_null_is_rejected_not_a_500(test_tenant, test_superadmin, pool):
    did = f"1555{uuid.uuid4().int % 10**7:07d}"
    try:
        async with _client(test_superadmin["token"]) as su:
            created = await su.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})
            resp = await su.patch(f"/phone-numbers/{created.json()['id']}", json={"did": None})
        assert resp.status_code == 422, resp.text
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)
        await cache.invalidate(f"did:{did}")
