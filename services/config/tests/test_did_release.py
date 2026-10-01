"""A DID is unique among live numbers only: deleting one releases it for any
tenant to add again, and the deleted row stays as ownership history."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_target_tenant
from services.config import cache, phone_numbers
from services.config.app import app
from services.config.phone_numbers import DidAlreadyAssigned


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Other Tenant {slug}", slug,
    ))
    yield row
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", row["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", row["id"])


@pytest.fixture
def did():
    return f"test-did-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def cleanup(pool, did):
    yield
    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)
    await cache.invalidate(f"did:{did}")


async def _add(tenant, did):
    set_target_tenant(tenant["id"])
    return await phone_numbers.create_phone_number(tenant_id=tenant["id"], did=did)


async def _remove(tenant, row):
    set_target_tenant(tenant["id"])
    await phone_numbers.soft_delete_phone_number(row["id"])


async def test_live_did_cannot_be_added_twice_by_same_tenant(test_tenant, did, cleanup):
    await _add(test_tenant, did)
    with pytest.raises(DidAlreadyAssigned):
        await _add(test_tenant, did)


async def test_live_did_cannot_be_taken_by_another_tenant(test_tenant, other_tenant, did, cleanup):
    await _add(test_tenant, did)
    with pytest.raises(DidAlreadyAssigned) as exc:
        await _add(other_tenant, did)
    assert test_tenant["slug"] not in str(exc.value) and test_tenant["name"] not in str(exc.value)


async def test_deleted_did_can_be_readded_by_same_tenant_and_history_is_kept(test_tenant, did, cleanup, pool):
    first = await _add(test_tenant, did)
    await _remove(test_tenant, first)

    second = await _add(test_tenant, did)

    assert second["id"] != first["id"]
    rows = await pool.fetch("SELECT id, deleted_at FROM phone_numbers WHERE did = $1 ORDER BY created_at", did)
    assert [(r["id"], r["deleted_at"] is None) for r in rows] == [(first["id"], False), (second["id"], True)]


async def test_deleted_did_can_be_taken_by_another_tenant_and_routes_there(
    test_tenant, other_tenant, did, cleanup,
):
    first = await _add(test_tenant, did)
    await _remove(test_tenant, first)

    await _add(other_tenant, did)

    route = await cache.get_json(f"did:{did}")
    assert route["tenant_slug"] == other_tenant["slug"]
    assert (await phone_numbers.get_by_did(did))["tenant_slug"] == other_tenant["slug"]


async def test_concurrent_adds_of_the_same_did_let_exactly_one_win(test_tenant, other_tenant, did, cleanup, pool):
    async def add_in_own_task(tenant):
        return await _add(tenant, did)

    results = await asyncio.gather(
        add_in_own_task(test_tenant), add_in_own_task(other_tenant), return_exceptions=True,
    )

    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, DidAlreadyAssigned) for r in results) == 1
    assert await pool.fetchval("SELECT count(*) FROM phone_numbers WHERE did = $1 AND deleted_at IS NULL", did) == 1


async def test_changing_a_number_to_a_live_did_is_rejected(test_tenant, did, cleanup, pool):
    await _add(test_tenant, did)
    other_did = f"test-did-{uuid.uuid4().hex[:8]}"
    movable = await _add(test_tenant, other_did)
    try:
        set_target_tenant(test_tenant["id"])
        with pytest.raises(DidAlreadyAssigned):
            await phone_numbers.update_phone_number(movable["id"], did=did)
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", other_did)
        await cache.invalidate(f"did:{other_did}")


async def test_api_returns_409_without_naming_the_owner(test_tenant, other_tenant, test_superadmin, did, cleanup):
    await _add(test_tenant, did)
    headers = {"Authorization": f"Bearer {test_superadmin['token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        resp = await client.post(f"/tenants/{other_tenant['id']}/phone-numbers", json={"did": did})
    assert resp.status_code == 409
    assert test_tenant["slug"] not in resp.text and test_tenant["name"] not in resp.text
