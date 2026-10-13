from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import agents, cache, provider_configs
from services.config.app import app


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant(pool):
    """A second tenant for cross-tenant isolation tests."""
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        f"Other Tenant {slug}", slug,
    )
    tenant = dict(row)
    yield tenant
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_create_and_get_agent(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
        greeting="Hi there", system_prompt="Be helpful.",
    )
    assert created["slug"] == "support-agent"
    assert created["transfer_type"] == "none"  # schema default
    assert created["config_version"] == 1

    fetched = await agents.get_agent(test_tenant["slug"], "support-agent")
    assert fetched is not None
    assert fetched["id"] == created["id"]


async def test_get_agent_returns_none_for_wrong_tenant(test_tenant, scoped):
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    assert await agents.get_agent("not-a-real-tenant-slug", "support-agent") is None


async def test_create_agent_warms_cache_when_tenant_slug_given(test_tenant, scoped):
    await cache.invalidate(f"agent:{test_tenant['slug']}:warm-test")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="warm-test", name="Warm Test",
        tenant_slug=test_tenant["slug"],
    )
    assert await cache.get_json(f"agent:{test_tenant['slug']}:warm-test") is not None


async def test_create_agent_without_tenant_slug_does_not_warm_cache(test_tenant, scoped):
    await cache.invalidate(f"agent:{test_tenant['slug']}:no-warm-test")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="no-warm-test", name="No Warm Test",
    )
    assert await cache.get_json(f"agent:{test_tenant['slug']}:no-warm-test") is None


async def test_update_agent_transfer_config_and_invalidates_cache(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    # Warm the cache.
    await agents.get_agent(test_tenant["slug"], "support-agent")
    assert await cache.get_json(f"agent:{test_tenant['slug']}:support-agent") is not None

    updated = await agents.update_agent(
        created["id"], tenant_slug=test_tenant["slug"],
        transfer_type="warm", transfer_destination="+18005550100", queue_id="support-queue-1",
    )
    assert updated["transfer_type"] == "warm"
    assert updated["transfer_destination"] == "+18005550100"
    assert updated["config_version"] == created["config_version"] + 1

    assert await cache.get_json(f"agent:{test_tenant['slug']}:support-agent") is None


async def test_update_agent_rejects_unknown_field(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    with pytest.raises(ValueError):
        await agents.update_agent(
            created["id"], tenant_slug=test_tenant["slug"], not_a_real_column="x",
        )


async def test_soft_delete_agent_excluded_from_get(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
    assert await agents.get_agent(test_tenant["slug"], "support-agent") is None


async def test_update_agent_rejects_cross_tenant_hijack(test_tenant, scoped, other_tenant):
    """Updating an agent via another tenant's slug is indistinguishable from not found."""
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Original",
    )

    with pytest.raises(LookupError):
        await agents.update_agent(
            created["id"], tenant_slug=other_tenant["slug"], name="Hijacked",
        )

    # Untouched — still visible, unchanged, under its real tenant.
    unchanged = await agents.get_agent(test_tenant["slug"], "support-agent")
    assert unchanged["name"] == "Original"


async def test_soft_delete_agent_rejects_cross_tenant_deletion(test_tenant, scoped, other_tenant):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )

    with pytest.raises(LookupError):
        await agents.soft_delete_agent(created["id"], tenant_slug=other_tenant["slug"])

    # Still alive under its real tenant.
    assert await agents.get_agent(test_tenant["slug"], "support-agent") is not None


async def test_soft_delete_agent_rejects_a_live_call(test_tenant, scoped, pool):
    # No force override here, unlike the tenant/provider guards.
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    session_id = f"test-live-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, ended_at) "
        "VALUES ($1, $2, 'inbound', $3, NULL)",
        session_id, test_tenant["slug"], created["id"],
    )
    try:
        with pytest.raises(agents.AgentHasLiveCalls) as exc_info:
            await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
        assert exc_info.value.live_call_count == 1

        # Untouched — still there.
        assert await agents.get_agent(test_tenant["slug"], "support-agent") is not None
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_soft_delete_agent_allowed_once_call_ends(test_tenant, scoped, pool):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    session_id = f"test-ended-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, ended_at) "
        "VALUES ($1, $2, 'inbound', $3, NOW())",
        session_id, test_tenant["slug"], created["id"],
    )
    try:
        await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
        assert await agents.get_agent(test_tenant["slug"], "support-agent") is None
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


# ── Language modes ───────────────────────────────────────────────────────────

async def _cartesia(tenant_id):
    return str((await provider_configs.create_provider_config(
        tenant_id=tenant_id, name="Cartesia", role="tts", engine="cartesia", voice="v",
        allow_pointer_schemes=False,
    ))["id"])


async def _agent_row(pool, agent_id):
    return await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)


@pytest.mark.parametrize(("fields", "message"), [
    ({"language": "en", "supported_languages": ["en", "hi", "es"], "language_mode": "multilingual"},
     "at most 2 languages"),
    ({"language": "en", "language_mode": "multilingual"}, "needs a second language"),
    ({"language": "en", "supported_languages": ["en", "hi", "es"]}, "at most 2 languages"),
    ({"language_mode": "monolingual"}, "exactly 1 language"),
    ({"language": "en", "supported_languages": ["en", "hi"], "language_mode": "monolingual"},
     "exactly 1 language"),
])
async def test_create_rejects_a_language_count_the_mode_does_not_allow(test_tenant, scoped, fields, message):
    tts = await _cartesia(test_tenant["id"])
    with pytest.raises(ValueError, match=message):
        await agents.create_agent(
            tenant_id=test_tenant["id"], slug="bad-mode", name="Bad", tts_config_id=tts, **fields,
        )


async def test_unknown_language_mode_is_a_422():
    from services.config.schemas import AgentCreate, AgentUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AgentCreate(slug="a", name="A", language_mode="trilingual")
    with pytest.raises(ValidationError):
        AgentUpdate(language_mode="trilingual")


async def test_monolingual_save_clears_the_per_language_columns(test_tenant, scoped, pool):
    tts = await _cartesia(test_tenant["id"])
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="mono", name="Mono", tts_config_id=tts,
        language="en", supported_languages=["en", "hi"], greeting_by_language={"hi": "namaste"},
        tts_config_by_language={"hi": tts},
    )
    assert created["language_mode"] == "multilingual"
    await agents.update_agent(
        created["id"], tenant_slug=test_tenant["slug"], language_mode="monolingual",
    )
    row = await _agent_row(pool, created["id"])
    assert row["language_mode"] == "monolingual"
    assert row["language"] == "en"
    assert row["supported_languages"] is None
    assert row["tts_config_by_language"] is None
    assert row["greeting_by_language"] is None


async def test_a_stored_mode_cannot_be_cleared(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="moded", name="Moded", language="en", language_mode="monolingual",
    )
    with pytest.raises(ValueError, match="can't be cleared"):
        await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], language_mode=None)


async def test_a_mode_is_derived_from_the_language_count(test_tenant, scoped):
    tts = await _cartesia(test_tenant["id"])
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="derive", name="Derive", tts_config_id=tts, language="en",
    )
    assert created["language_mode"] is None
    two = await agents.update_agent(
        created["id"], tenant_slug=test_tenant["slug"], supported_languages=["en", "hi"],
    )
    assert two["language_mode"] == "multilingual"
    with pytest.raises(ValueError, match="at most 2 languages"):
        await agents.update_agent(
            created["id"], tenant_slug=test_tenant["slug"], supported_languages=["en", "hi", "es"],
        )


async def test_a_language_only_patch_cannot_grow_a_multilingual_agent_past_two(test_tenant, scoped, pool):
    tts = await _cartesia(test_tenant["id"])
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="grow", name="Grow", tts_config_id=tts,
        language="en", supported_languages=["en", "hi"],
    )
    with pytest.raises(ValueError, match="at most 2 languages"):
        await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], language="es")
    assert list((await _agent_row(pool, created["id"]))["supported_languages"]) == ["en", "hi"]


async def test_a_legacy_three_language_agent_still_takes_an_unrelated_edit(test_tenant, scoped, pool):
    tts = await _cartesia(test_tenant["id"])
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="legacy3", name="Legacy", tts_config_id=tts, language="en",
    )
    await pool.execute(
        "UPDATE agents SET supported_languages = ARRAY['en','hi','es'] WHERE id = $1", created["id"],
    )
    updated = await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], name="Renamed")
    assert updated["name"] == "Renamed"
    assert updated["language_mode"] is None


async def test_provider_edit_revalidates_moded_agents_with_the_real_query(test_tenant, scoped):
    base = await _cartesia(test_tenant["id"])
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml", name="ML", tts_config_id=base,
        language="en", supported_languages=["en", "hi"],
    )
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="mono-hi", name="Mono", tts_config_id=base,
        language="hi", language_mode="monolingual",
    )
    await provider_configs.update_provider_config(base, allow_pointer_schemes=False, name="Renamed voice")


async def test_template_create_is_monolingual(pool, test_tenant, test_admin):
    llm = await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
        "VALUES ($1, 'llm cfg', 'llm', 'openai', 'ref') RETURNING id", test_tenant["id"],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    ) as client:
        resp = await client.post(f"/tenants/{test_tenant['slug']}/agents/from-template", json={
            "template_id": "faq-support", "template_version": 4, "name": "Front Desk",
            "business_name": "Acme Dental", "business_facts": "Open 9 to 5.", "llm_config_id": str(llm),
        })
    assert resp.status_code == 201, resp.text
    row = await _agent_row(pool, uuid.UUID(resp.json()["id"]))
    assert row["language_mode"] == "monolingual"
    assert row["language"] == "en"
    assert row["supported_languages"] is None
