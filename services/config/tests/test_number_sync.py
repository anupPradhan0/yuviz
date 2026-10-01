"""Provider inbound sync on phone-number create/update/delete, driven by the
scriptable FakeProvider (libs/telephony_sdk/providers/fake.py)."""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from libs.telephony_sdk.providers import fake
from services.config import cache, number_sync
from services.config.app import app

BASE = "https://calls.example.test"


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    monkeypatch.setenv("TELEPHONY_PUBLIC_BASE_URL", BASE)
    fake.SYNC_LOG.clear()


@pytest_asyncio.fixture
async def client(test_superadmin):
    headers = {"Authorization": f"Bearer {test_superadmin['token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c


async def _fake_config(pool, tenant, **credentials):
    config_id = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Fake', 'fake', $2::jsonb) RETURNING id",
        tenant["id"], json.dumps(credentials),
    )
    return str(config_id)


@pytest_asyncio.fixture
async def cleanup(pool, test_tenant):
    yield
    rows = await pool.fetch("SELECT did FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", test_tenant["id"])
    await pool.execute("DELETE FROM telephony_configs WHERE tenant_id = $1", test_tenant["id"])
    for r in rows:
        await cache.invalidate(f"did:{r['did']}")


def _did():
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


async def _add(client, tenant, config_id, did):
    return await client.post(f"/tenants/{tenant['id']}/phone-numbers", json={"did": did, "telephony_config_id": config_id})


async def test_adding_a_number_points_it_at_the_platform_and_stores_the_app_id(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    did = _did()
    resp = await _add(client, test_tenant, config_id, did)

    assert resp.status_code == 201, resp.text
    sync = resp.json()["provider_sync"]
    assert (sync["ok"], sync["message"]) == (True, None) and sync["at"]
    (action, number, urls), = [e for e in fake.SYNC_LOG if e[0] == "attach"]
    assert (action, number) == ("attach", did)
    assert urls.answer_url == f"{BASE}/fake/voice/{config_id}"
    assert urls.hangup_url == f"{BASE}/fake/status/{config_id}"
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    assert creds["inbound_application_id"].startswith("fake-app-")


async def test_a_number_not_in_the_account_is_refused_and_not_saved(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, owns_number="no")
    did = _did()
    resp = await _add(client, test_tenant, config_id, did)
    assert resp.status_code == 400
    assert await pool.fetchval("SELECT count(*) FROM phone_numbers WHERE did = $1", did) == 0
    assert fake.SYNC_LOG == []


async def test_an_unreachable_provider_refuses_the_add(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, owns_number="error")
    did = _did()
    resp = await _add(client, test_tenant, config_id, did)
    assert resp.status_code == 502
    assert await pool.fetchval("SELECT count(*) FROM phone_numbers WHERE did = $1", did) == 0


async def test_a_failed_attach_keeps_the_number_and_reports_why(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, attach_ok=False)
    resp = await _add(client, test_tenant, config_id, _did())
    assert resp.status_code == 201
    sync = resp.json()["provider_sync"]
    assert (sync["ok"], sync["message"]) == (False, "fake: scripted attach failure")


async def test_without_a_public_base_url_the_number_is_saved_with_a_clear_warning(client, test_tenant, pool, cleanup, monkeypatch):
    monkeypatch.delenv("TELEPHONY_PUBLIC_BASE_URL")
    config_id = await _fake_config(pool, test_tenant)
    resp = await _add(client, test_tenant, config_id, _did())
    assert resp.status_code == 201
    assert resp.json()["provider_sync"]["ok"] is False
    assert "TELEPHONY_PUBLIC_BASE_URL" in resp.json()["provider_sync"]["message"]
    assert fake.SYNC_LOG == []


async def test_delete_detaches_first_and_keeps_the_row_if_the_provider_refuses(client, test_tenant, pool, cleanup):
    refusing = await _fake_config(pool, test_tenant, detach_ok=False)
    did = _did()
    number_id = (await _add(client, test_tenant, refusing, did)).json()["id"]
    resp = await client.delete(f"/phone-numbers/{number_id}")
    assert resp.status_code == 502
    assert await pool.fetchval("SELECT deleted_at FROM phone_numbers WHERE id = $1", number_id) is None

    await pool.execute("UPDATE telephony_configs SET credentials = '{}'::jsonb WHERE id = $1", refusing)
    await cache.invalidate(f"telephony_config:{refusing}")
    fake.SYNC_LOG.clear()
    resp = await client.delete(f"/phone-numbers/{number_id}")
    assert resp.status_code == 204
    assert [(a, n) for a, n, _ in fake.SYNC_LOG if a in ("attach", "detach")] == [("detach", did)]


async def test_changing_the_did_detaches_the_old_number_and_attaches_the_new(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    old, new = _did(), _did()
    number_id = (await _add(client, test_tenant, config_id, old)).json()["id"]
    fake.SYNC_LOG.clear()

    resp = await client.patch(f"/phone-numbers/{number_id}", json={"did": new})

    assert resp.status_code == 200
    assert resp.json()["provider_sync"]["ok"] is True
    assert [(a, n) for a, n, _ in fake.SYNC_LOG if a in ("attach", "detach")] == [("detach", old), ("attach", new)]


async def test_routing_only_changes_do_not_touch_the_provider(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    number_id = (await _add(client, test_tenant, config_id, _did())).json()["id"]
    fake.SYNC_LOG.clear()
    resp = await client.patch(f"/phone-numbers/{number_id}", json={"status": "inactive"})
    assert resp.status_code == 200
    assert fake.SYNC_LOG == []


async def test_resync_reattaches_every_number_and_creates_the_app_once(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    dids = [_did() for _ in range(4)]
    for d in dids:
        # Inserted directly: nothing has created the provider app yet.
        await pool.execute(
            "INSERT INTO phone_numbers (tenant_id, did, telephony_config_id) VALUES ($1, $2, $3)",
            test_tenant["id"], d, config_id,
        )

    resp = await client.post(f"/telephony-configs/{config_id}/sync-numbers")

    assert resp.status_code == 200
    assert sorted(r["did"] for r in resp.json()["results"]) == sorted(dids)
    assert all(r["ok"] for r in resp.json()["results"])
    assert sorted(n for a, n, _ in fake.SYNC_LOG if a == "attach") == sorted(dids)
    created = [app for a, _, app in fake.SYNC_LOG if a == "create_app"]
    assert len(created) == 1
    assert [a for a, _, _ in fake.SYNC_LOG if a == "refresh_app"] == []
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    assert creds["inbound_application_id"] == created[0]


async def test_sync_outcome_is_stored_on_the_number_and_retry_updates_it(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, attach_ok=False)
    number_id = (await _add(client, test_tenant, config_id, _did())).json()["id"]

    listed = (await client.get(f"/tenants/{test_tenant['id']}/phone-numbers")).json()
    stored = next(n for n in listed if n["id"] == number_id)["provider_sync"]
    assert stored["ok"] is False and stored["message"] == "fake: scripted attach failure" and stored["at"]

    await pool.execute("UPDATE telephony_configs SET credentials = '{}'::jsonb WHERE id = $1", config_id)
    await cache.invalidate(f"telephony_config:{config_id}")
    retried = await client.post(f"/phone-numbers/{number_id}/sync")
    assert retried.status_code == 200
    assert retried.json()["provider_sync"]["ok"] is True
    got = (await client.get(f"/phone-numbers/{number_id}")).json()
    assert got["provider_sync"]["ok"] is True


async def test_retry_on_a_number_with_nothing_to_sync_is_400(client, test_tenant, pool, cleanup):
    did = _did()
    number_id = (await client.post(f"/tenants/{test_tenant['id']}/phone-numbers", json={"did": did})).json()["id"]
    assert (await client.post(f"/phone-numbers/{number_id}/sync")).status_code == 400


async def test_resaving_credentials_keeps_the_platform_created_app_id(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, inbound_application_id="fake-app-1")
    resp = await client.patch(f"/telephony-configs/{config_id}", json={"credentials": {"note": "edited"}})
    assert resp.status_code == 200, resp.text
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    assert creds == {"note": "edited", "inbound_application_id": "fake-app-1"}


async def test_resending_the_same_config_id_does_not_touch_the_provider(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    number_id = (await _add(client, test_tenant, config_id, _did())).json()["id"]
    fake.SYNC_LOG.clear()
    resp = await client.patch(f"/phone-numbers/{number_id}", json={"telephony_config_id": config_id, "region": "x"})
    assert resp.status_code == 200
    assert fake.SYNC_LOG == []


async def test_force_removes_a_number_the_provider_wont_detach(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant, detach_ok=False)
    number_id = (await _add(client, test_tenant, config_id, _did())).json()["id"]
    assert (await client.delete(f"/phone-numbers/{number_id}")).status_code == 502
    assert (await client.delete(f"/phone-numbers/{number_id}?force=true")).status_code == 204
    assert await pool.fetchval("SELECT deleted_at FROM phone_numbers WHERE id = $1", number_id) is not None


async def test_a_concurrent_first_sync_reuses_the_stored_app_and_discards_its_own(test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    stale = await number_sync.telephony_configs.get_telephony_config(config_id, platform_scoped=True)
    first, second = _did(), _did()

    # Both start from the same config read with no app id, as two concurrent syncs would.
    await number_sync.attach(stale, first)
    outcome = await number_sync.attach(stale, second)

    assert outcome["ok"] is True
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    created = [app for a, _, app in fake.SYNC_LOG if a == "create_app"]
    discarded = [app for a, _, app in fake.SYNC_LOG if a == "discard_app"]
    assert len(created) == 2 and discarded == [created[1]]
    assert creds["inbound_application_id"] == created[0]
    assert [n for a, n, _ in fake.SYNC_LOG if a == "attach"] == [first, second, second]


async def test_a_config_with_numbers_cannot_be_deleted(client, test_tenant, pool, cleanup):
    config_id = await _fake_config(pool, test_tenant)
    number_id = (await _add(client, test_tenant, config_id, _did())).json()["id"]
    resp = await client.delete(f"/telephony-configs/{config_id}")
    assert resp.status_code == 409 and resp.json()["number_count"] == 1
    assert (await client.delete(f"/phone-numbers/{number_id}")).status_code == 204
    assert (await client.delete(f"/telephony-configs/{config_id}")).status_code == 204


async def test_a_new_provider_account_does_not_inherit_the_old_accounts_app(client, test_tenant, pool, cleanup, monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
    config_id = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'Vobiz', 'vobiz', $2::jsonb) RETURNING id",
        test_tenant["id"], json.dumps({"auth_id": "MA_A", "auth_token": "t", "inbound_application_id": "app-A"}),
    )
    same = await client.patch(f"/telephony-configs/{config_id}", json={"credentials": {"auth_id": "MA_A", "auth_token": "t2"}})
    assert same.status_code == 200, same.text
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    assert creds["inbound_application_id"] == "app-A"

    other = await client.patch(f"/telephony-configs/{config_id}", json={"credentials": {"auth_id": "MA_B", "auth_token": "t3"}})
    assert other.status_code == 200, other.text
    creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
    assert "inbound_application_id" not in creds
