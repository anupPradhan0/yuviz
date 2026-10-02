"""
Seeded superadmin, public signup with code verification, and change-email.
Each test gets a throwaway database: "no superadmin yet" is database-wide.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import auth, db
from services.config import users as users_service
from services.config.app import AcceptThrottle, app

REPO_ROOT = Path(__file__).resolve().parents[3]
# Same files, same order as deployment/sh/init.sh: rls.sql grants the
# yuviz_app/yuviz_platform roles that platform_conn switches to.
SCHEMA_FILES = ["schema.sql", "knowledge_schema.sql", "telephony_schema.sql", "rls.sql"]
# Roles are cluster-wide: never plant a known password if rls.sql creates yuviz_app here.
_APP_PASSWORD = secrets.token_urlsafe(24)


async def _apply_schemas(dsn: str) -> None:
    for name in SCHEMA_FILES:
        proc = await asyncio.create_subprocess_exec(
            "psql", dsn, "-v", "ON_ERROR_STOP=1", "-v", f"yuviz_app_password={_APP_PASSWORD}", "-q",
            "-f", str(REPO_ROOT / "database" / name),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        assert proc.returncode == 0, f"{name} failed to apply: {err.decode()}"

SUPERADMIN = {"email": "root@example.com", "password": "a-real-password"}


def _signup(email: str = "owner@acme.com", **overrides) -> dict:
    return {
        "organization_name": "Acme Corp",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": email,
        "phone": "+1 9876543210",
        "password": "a-real-password",
        "signup_source": "linkedin",
        **overrides,
    }


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_db():
    """An empty, schema-applied database installed as db.py's process-wide
    pool; the original pool is restored afterwards."""
    base_dsn = os.environ["POSTGRES_DSN"]
    name = f"yuviz_signup_test_{uuid.uuid4().hex[:12]}"

    # CREATE DATABASE can't run in a transaction — standalone connection.
    conn = await asyncpg.connect(base_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    test_dsn = urlunsplit(urlsplit(base_dsn)._replace(path=f"/{name}"))
    await _apply_schemas(test_dsn)
    test_pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=10)

    original = db._pool  # noqa: SLF001
    db._pool = test_pool  # noqa: SLF001
    try:
        yield test_pool
    finally:
        db._pool = original  # noqa: SLF001
        await test_pool.close()
        conn = await asyncpg.connect(base_dsn)
        try:
            await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def anon_client(fresh_db):
    app.state.register_throttle = AcceptThrottle()
    app.state.verify_throttle = AcceptThrottle()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def mailer():
    """Every verification email is captured here instead of sent."""
    with patch("services.config.email.send_verification_code_email", AsyncMock()) as mock:
        yield mock


@pytest.fixture(autouse=True)
def exists_mailer():
    with patch("services.config.email.send_account_exists_email", AsyncMock()) as mock:
        yield mock


def _code(mailer, to: str | None = None) -> str:
    call = mailer.call_args
    if to is not None:
        assert call.kwargs["to_email"].lower() == to.lower()
    return call.kwargs["code"]


async def _register(client, email: str = "owner@acme.com", **overrides) -> dict:
    """Full signup: register, then verify with the emailed code."""
    with patch("services.config.email.send_verification_code_email", AsyncMock()) as mock:
        resp = await client.post("/auth/register", json=_signup(email, **overrides))
        assert resp.status_code == 202, resp.text
        verified = await client.post("/auth/verify-email", json={"email": email, "code": _code(mock, email)})
    assert verified.status_code == 200, verified.text
    return verified.json()


async def _allow_resend(pool, email: str) -> None:
    await pool.execute(
        "UPDATE pending_registrations SET last_sent_at = now() - interval '2 minutes' WHERE email = $1",
        email.lower(),
    )


class TestSeedSuperadmin:
    async def test_creates_a_platform_superadmin(self, anon_client):
        user = await users_service.seed_superadmin(**SUPERADMIN)
        assert user["role"] == "superadmin"
        assert user["tenant_id"] is None
        resp = await anon_client.post("/auth/login", json=SUPERADMIN)
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "superadmin"

    async def test_restart_never_duplicates_or_resets_it(self, fresh_db, anon_client):
        await users_service.seed_superadmin(**SUPERADMIN)
        again = await users_service.seed_superadmin(email="other@example.com", password="other-password")
        assert again is None
        assert await fresh_db.fetchval("SELECT count(*) FROM users WHERE role = 'superadmin'") == 1
        assert (await anon_client.post("/auth/login", json=SUPERADMIN)).status_code == 200

    async def test_changed_credentials_survive_a_reseed(self, anon_client, mailer):
        await users_service.seed_superadmin(**SUPERADMIN)
        token = (await anon_client.post("/auth/login", json=SUPERADMIN)).json()["access_token"]
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": SUPERADMIN["password"], "new_email": "boss@example.com"},
            headers=_bearer(token),
        )
        assert resp.status_code == 202
        resp = await anon_client.post(
            "/auth/change-email/confirm", json={"code": _code(mailer, "boss@example.com")}, headers=_bearer(token),
        )
        assert resp.status_code == 200
        assert await users_service.seed_superadmin(**SUPERADMIN) is None
        assert (await anon_client.post("/auth/login", json=SUPERADMIN)).status_code == 401
        moved = {"email": "boss@example.com", "password": SUPERADMIN["password"]}
        assert (await anon_client.post("/auth/login", json=moved)).status_code == 200

    async def test_concurrent_seeds_create_exactly_one(self, fresh_db):
        results = await asyncio.gather(*(
            users_service.seed_superadmin(email=f"racer{i}@example.com", password="a-real-password")
            for i in range(10)
        ))
        assert len([r for r in results if r is not None]) == 1
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1


class TestRegister:
    async def test_creates_an_admin_in_a_new_organization(self, fresh_db, anon_client):
        body = await _register(anon_client)
        user = body["user"]
        assert user["role"] == "admin"
        assert user["tenant_id"] is not None
        assert (user["first_name"], user["last_name"]) == ("Jane", "Doe")
        assert user["phone"] == "+1 9876543210"
        assert user["signup_source"] == "linkedin"
        assert "password_hash" not in user
        tenant = await fresh_db.fetchrow("SELECT name, slug FROM tenants WHERE id = $1", uuid.UUID(user["tenant_id"]))
        assert tenant["name"] == "Acme Corp"
        assert tenant["slug"] == "acme-corp"

        me = await anon_client.get("/auth/me", headers=_bearer(body["access_token"]))
        assert me.status_code == 200
        assert me.json()["email"] == "owner@acme.com"

    async def test_open_while_a_superadmin_exists(self, anon_client):
        await users_service.seed_superadmin(**SUPERADMIN)
        assert (await _register(anon_client))["user"]["role"] == "admin"

    async def test_client_cannot_choose_role_or_tenant(self, fresh_db, anon_client):
        other = await fresh_db.fetchval("INSERT INTO tenants (name, slug) VALUES ('Other', 'other') RETURNING id")
        body = await _register(anon_client, role="superadmin", tenant_id=str(other))
        assert body["user"]["role"] == "admin"
        assert body["user"]["tenant_id"] != str(other)
        assert auth.decode_access_token(body["access_token"]).role == "admin"
        assert await fresh_db.fetchval("SELECT count(*) FROM users WHERE role = 'superadmin'") == 0

    async def test_same_organization_name_gets_a_distinct_tenant(self, fresh_db, anon_client):
        first = await _register(anon_client, "a@acme.com")
        second = await _register(anon_client, "b@acme.com")
        assert first["user"]["tenant_id"] != second["user"]["tenant_id"]
        slugs = await fresh_db.fetch("SELECT slug FROM tenants WHERE name = 'Acme Corp'")
        assert len({r["slug"] for r in slugs}) == 2

    async def test_duplicate_email_gets_a_notice_not_a_code_and_writes_nothing(
        self, fresh_db, anon_client, mailer, exists_mailer,
    ):
        await _register(anon_client)
        resp = await anon_client.post("/auth/register", json=_signup("OWNER@acme.com", organization_name="Dup"))
        assert resp.status_code == 202
        mailer.assert_not_called()
        assert exists_mailer.call_args.kwargs["to_email"] == "OWNER@acme.com"
        assert await fresh_db.fetchval("SELECT count(*) FROM tenants WHERE name = 'Dup'") == 0
        assert await fresh_db.fetchval("SELECT count(*) FROM pending_registrations") == 0
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1

    async def test_taken_and_new_emails_get_the_same_responses(self, anon_client):
        await _register(anon_client, "taken@acme.com")
        taken = [(await anon_client.post("/auth/register", json=_signup("taken@acme.com"))) for _ in range(2)]
        new = [(await anon_client.post("/auth/register", json=_signup("new@acme.com"))) for _ in range(2)]
        assert [r.status_code for r in taken] == [r.status_code for r in new] == [202, 202]
        assert [r.json()["verification_required"] for r in taken + new] == [True] * 4

    @pytest.mark.parametrize(
        "overrides",
        [
            {"organization_name": ""},
            {"first_name": ""},
            {"email": "not-an-email"},
            {"phone": "12345"},
            {"password": "short"},
            {"signup_source": "carrier-pigeon"},
        ],
        ids=["no-org", "no-first-name", "bad-email", "bad-phone", "short-password", "unknown-source"],
    )
    async def test_rejects_invalid_input(self, fresh_db, anon_client, overrides):
        resp = await anon_client.post("/auth/register", json=_signup(**overrides))
        assert resp.status_code == 422
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_is_rate_limited_per_client(self, anon_client):
        codes = [
            (await anon_client.post("/auth/register", json=_signup(f"u{i}@acme.com"))).status_code
            for i in range(11)
        ]
        assert codes[:10] == [202] * 10
        assert codes[10] == 429


class TestRegisteredAdminPermissions:
    async def test_sees_only_their_own_organization(self, anon_client):
        a = await _register(anon_client, "a@acme.com", organization_name="Acme")
        b = await _register(anon_client, "b@globex.com", organization_name="Globex")
        headers = _bearer(a["access_token"])

        users = await anon_client.get("/users", headers=headers)
        assert {u["email"] for u in users.json()} == {"a@acme.com"}
        tenants = await anon_client.get("/tenants", headers=headers)
        assert [t["id"] for t in tenants.json()] == [a["user"]["tenant_id"]]
        cross = await anon_client.get("/users", params={"tenant_id": b["user"]["tenant_id"]}, headers=headers)
        assert "b@globex.com" not in {u["email"] for u in cross.json()}

    async def test_invites_users_into_own_organization_only(self, anon_client):
        a = await _register(anon_client, "a@acme.com", organization_name="Acme")
        b = await _register(anon_client, "b@globex.com", organization_name="Globex")
        headers = _bearer(a["access_token"])
        own = a["user"]["tenant_id"]

        with patch("services.config.email.send_invite_email") as mock_send:
            ok = await anon_client.post(
                "/invites", json={"email": "agent@acme.com", "role": "viewer", "tenant_id": own}, headers=headers,
            )
            cross = await anon_client.post(
                "/invites", json={"email": "x@globex.com", "role": "viewer", "tenant_id": b["user"]["tenant_id"]},
                headers=headers,
            )
            escalate = await anon_client.post(
                "/invites", json={"email": "root2@acme.com", "role": "superadmin", "tenant_id": own},
                headers=headers,
            )
        assert ok.status_code == 201
        assert cross.status_code == 403
        assert escalate.status_code == 403

        accepted = await anon_client.post(
            "/invites/accept", json={"password": "another-password"},
            headers={"X-Invite-Token": mock_send.call_args_list[0].kwargs["raw_token"]},
        )
        assert accepted.status_code == 200
        assert accepted.json()["role"] == "viewer"
        assert accepted.json()["tenant_id"] == own


class TestSuperadminIsSeedOnly:
    async def test_superadmin_cannot_invite_another_superadmin(self, anon_client):
        await users_service.seed_superadmin(**SUPERADMIN)
        token = (await anon_client.post("/auth/login", json=SUPERADMIN)).json()["access_token"]
        with patch("services.config.email.send_invite_email"):
            resp = await anon_client.post(
                "/invites", json={"email": "second@example.com", "role": "superadmin"}, headers=_bearer(token),
            )
        assert resp.status_code == 403

    async def test_cannot_promote_a_user_to_superadmin(self, anon_client):
        await users_service.seed_superadmin(**SUPERADMIN)
        token = (await anon_client.post("/auth/login", json=SUPERADMIN)).json()["access_token"]
        admin = await _register(anon_client)
        resp = await anon_client.patch(
            f"/users/{admin['user']['id']}", json={"role": "superadmin"}, headers=_bearer(token),
        )
        assert resp.status_code == 422


class TestChangeEmail:
    async def test_wrong_password_is_rejected(self, anon_client):
        body = await _register(anon_client)
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": "wrong-password", "new_email": "new@acme.com"},
            headers=_bearer(body["access_token"]),
        )
        assert resp.status_code == 400

    async def test_missing_password_is_rejected(self, anon_client):
        body = await _register(anon_client)
        resp = await anon_client.post(
            "/auth/change-email", json={"new_email": "new@acme.com"}, headers=_bearer(body["access_token"]),
        )
        assert resp.status_code == 400

    async def test_taken_email_is_rejected(self, anon_client):
        await _register(anon_client, "taken@acme.com", organization_name="Taken")
        body = await _register(anon_client)
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": "a-real-password", "new_email": "Taken@acme.com"},
            headers=_bearer(body["access_token"]),
        )
        assert resp.status_code == 409

    async def test_email_changes_only_after_the_code_sent_to_the_new_address(self, anon_client, mailer):
        body = await _register(anon_client)
        headers = _bearer(body["access_token"])
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": "a-real-password", "new_email": "New@Acme.com"},
            headers=headers,
        )
        assert resp.status_code == 202
        code = _code(mailer, "new@acme.com")
        me = await anon_client.get("/auth/me", headers=headers)
        assert me.json()["email"] == "owner@acme.com"

        resp = await anon_client.post("/auth/change-email/confirm", json={"code": code}, headers=headers)
        assert resp.status_code == 200
        assert auth.decode_access_token(resp.json()["access_token"]).email == "new@acme.com"

    async def test_wrong_code_keeps_the_old_email(self, anon_client, mailer):
        body = await _register(anon_client)
        headers = _bearer(body["access_token"])
        await anon_client.post(
            "/auth/change-email",
            json={"current_password": "a-real-password", "new_email": "new@acme.com"},
            headers=headers,
        )
        resp = await anon_client.post(
            "/auth/change-email/confirm", json={"code": _wrong(_code(mailer))}, headers=headers,
        )
        assert resp.status_code == 400
        assert (await anon_client.get("/auth/me", headers=headers)).json()["email"] == "owner@acme.com"

    async def test_send_failure_leaves_no_open_request(self, fresh_db, anon_client, mailer):
        body = await _register(anon_client)
        mailer.side_effect = OSError("relay down")
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": "a-real-password", "new_email": "new@acme.com"},
            headers=_bearer(body["access_token"]),
        )
        assert resp.status_code == 503
        assert await fresh_db.fetchval("SELECT count(*) FROM email_change_requests") == 0


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


class TestEmailVerification:
    async def test_register_creates_nothing_until_verified(self, fresh_db, anon_client, mailer):
        resp = await anon_client.post("/auth/register", json=_signup())
        assert resp.status_code == 202
        assert resp.json() == {"verification_required": True, "email": "owner@acme.com"}
        assert "access_token" not in resp.json()
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0
        assert await fresh_db.fetchval("SELECT count(*) FROM tenants WHERE name = 'Acme Corp'") == 0
        code = _code(mailer, "owner@acme.com")
        assert len(code) == 6 and code.isdigit()

    async def test_code_is_stored_hashed(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        stored = await fresh_db.fetchval("SELECT code_hash FROM pending_registrations")
        assert _code(mailer) not in stored

    async def test_smtp_failure_fails_signup_and_leaves_nothing(self, fresh_db, anon_client, mailer):
        mailer.side_effect = OSError("relay down")
        resp = await anon_client.post("/auth/register", json=_signup())
        assert resp.status_code == 503
        assert await fresh_db.fetchval("SELECT count(*) FROM pending_registrations") == 0
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_wrong_code_is_rejected(self, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        resp = await anon_client.post(
            "/auth/verify-email", json={"email": "owner@acme.com", "code": _wrong(_code(mailer))},
        )
        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"]

    async def test_expired_code_is_rejected(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        await fresh_db.execute("UPDATE pending_registrations SET expires_at = now() - interval '1 second'")
        resp = await anon_client.post("/auth/verify-email", json={"email": "owner@acme.com", "code": _code(mailer)})
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"]
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_code_dies_after_too_many_wrong_attempts(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        code = _code(mailer)
        for _ in range(5):
            await anon_client.post("/auth/verify-email", json={"email": "owner@acme.com", "code": _wrong(code)})
        resp = await anon_client.post("/auth/verify-email", json={"email": "owner@acme.com", "code": code})
        assert resp.status_code == 400
        assert "Too many" in resp.json()["detail"]
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_resend_issues_a_new_code_and_the_old_one_stops_working(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        old = _code(mailer)
        await _allow_resend(fresh_db, "owner@acme.com")
        resp = await anon_client.post("/auth/resend-code", json={"email": "owner@acme.com"})
        assert resp.status_code == 202
        new = _code(mailer)
        if old != new:
            stale = await anon_client.post("/auth/verify-email", json={"email": "owner@acme.com", "code": old})
            assert stale.status_code == 400
        ok = await anon_client.post("/auth/verify-email", json={"email": "owner@acme.com", "code": new})
        assert ok.status_code == 200

    async def test_resend_has_a_cooldown(self, anon_client):
        await anon_client.post("/auth/register", json=_signup())
        resp = await anon_client.post("/auth/resend-code", json={"email": "owner@acme.com"})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) > 0

    async def test_resend_is_capped_per_hour(self, fresh_db, anon_client):
        await anon_client.post("/auth/register", json=_signup())
        codes = []
        for _ in range(5):
            await _allow_resend(fresh_db, "owner@acme.com")
            codes.append((await anon_client.post("/auth/resend-code", json={"email": "owner@acme.com"})).status_code)
        assert codes == [202, 202, 202, 202, 429]

    async def test_resend_for_unknown_email_looks_the_same(self, anon_client, mailer):
        resp = await anon_client.post("/auth/resend-code", json={"email": "nobody@example.com"})
        assert resp.status_code == 202
        assert resp.json() == {"sent": True}
        mailer.assert_not_called()

    async def test_re_registering_replaces_the_pending_signup(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup(organization_name="First Try"))
        await _allow_resend(fresh_db, "owner@acme.com")
        resp = await anon_client.post("/auth/register", json=_signup(organization_name="Second Try"))
        assert resp.status_code == 202
        assert await fresh_db.fetchval("SELECT count(*) FROM pending_registrations") == 1
        verified = await anon_client.post(
            "/auth/verify-email", json={"email": "owner@acme.com", "code": _code(mailer)},
        )
        tenant = await fresh_db.fetchval(
            "SELECT name FROM tenants WHERE id = $1", uuid.UUID(verified.json()["user"]["tenant_id"]),
        )
        assert tenant == "Second Try"

    async def test_verified_user_logs_in_normally_afterwards(self, anon_client):
        await _register(anon_client)
        resp = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "a-real-password"})
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "admin"


class TestUnverifiedLogin:
    async def test_right_password_is_told_to_verify_and_gets_a_fresh_code(self, fresh_db, anon_client, mailer):
        await anon_client.post("/auth/register", json=_signup())
        await _allow_resend(fresh_db, "owner@acme.com")
        mailer.reset_mock()
        resp = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "a-real-password"})
        assert resp.status_code == 403
        assert "access_token" not in resp.json()
        mailer.assert_called_once()

    async def test_wrong_password_gets_the_normal_401(self, anon_client):
        await anon_client.post("/auth/register", json=_signup())
        resp = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "wrong-password"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid email or password"


class TestForgotPassword:
    async def _request_code(self, client, mailer, email: str = "owner@acme.com") -> str:
        mailer.reset_mock()
        resp = await client.post("/auth/forgot-password", json={"email": email})
        assert resp.status_code == 202
        return _code(mailer, email)

    async def test_code_sets_a_new_password_and_signs_in(self, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer, "OWNER@acme.com")
        resp = await anon_client.post(
            "/auth/reset-password",
            json={"email": "owner@acme.com", "code": code, "new_password": "brand-new-password"},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == "owner@acme.com"
        old = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "a-real-password"})
        new = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "brand-new-password"})
        assert old.status_code == 401
        assert new.status_code == 200

    async def test_code_works_only_once(self, fresh_db, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer)
        body = {"email": "owner@acme.com", "code": code, "new_password": "brand-new-password"}
        assert (await anon_client.post("/auth/reset-password", json=body)).status_code == 200
        assert (await anon_client.post("/auth/reset-password", json=body)).status_code == 400
        assert await fresh_db.fetchval("SELECT count(*) FROM password_reset_requests") == 0

    async def test_seeded_superadmin_can_reset(self, anon_client, mailer):
        await users_service.seed_superadmin(**SUPERADMIN)
        code = await self._request_code(anon_client, mailer, SUPERADMIN["email"])
        resp = await anon_client.post(
            "/auth/reset-password",
            json={"email": SUPERADMIN["email"], "code": code, "new_password": "brand-new-password"},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "superadmin"

    async def test_wrong_code_keeps_the_old_password(self, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer)
        resp = await anon_client.post(
            "/auth/reset-password",
            json={"email": "owner@acme.com", "code": _wrong(code), "new_password": "brand-new-password"},
        )
        assert resp.status_code == 400
        login = await anon_client.post("/auth/login", json={"email": "owner@acme.com", "password": "a-real-password"})
        assert login.status_code == 200

    async def test_code_dies_after_too_many_wrong_attempts(self, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer)
        body = {"email": "owner@acme.com", "new_password": "brand-new-password"}
        for _ in range(5):
            await anon_client.post("/auth/reset-password", json={**body, "code": _wrong(code)})
        resp = await anon_client.post("/auth/reset-password", json={**body, "code": code})
        assert resp.status_code == 400
        assert "Too many" in resp.json()["detail"]

    async def test_code_for_one_account_cannot_reset_another(self, anon_client, mailer):
        await _register(anon_client, "a@acme.com", organization_name="Acme")
        await _register(anon_client, "b@globex.com", organization_name="Globex")
        await self._request_code(anon_client, mailer, "b@globex.com")
        code = await self._request_code(anon_client, mailer, "a@acme.com")
        resp = await anon_client.post(
            "/auth/reset-password", json={"email": "b@globex.com", "code": code, "new_password": "brand-new-password"},
        )
        if resp.status_code == 200:  # both accounts drew the same 6 digits
            return
        assert resp.status_code == 400
        login = await anon_client.post("/auth/login", json={"email": "b@globex.com", "password": "a-real-password"})
        assert login.status_code == 200

    async def test_short_password_is_rejected(self, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer)
        resp = await anon_client.post(
            "/auth/reset-password", json={"email": "owner@acme.com", "code": code, "new_password": "short"},
        )
        assert resp.status_code == 422

    async def test_unknown_email_cooldown_and_send_failure_all_look_the_same(self, anon_client, mailer):
        await _register(anon_client)
        mailer.reset_mock()
        unknown = await anon_client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
        mailer.assert_not_called()
        first = await anon_client.post("/auth/forgot-password", json={"email": "owner@acme.com"})
        too_soon = await anon_client.post("/auth/forgot-password", json={"email": "owner@acme.com"})
        assert mailer.call_count == 1
        responses = [unknown, first, too_soon]
        assert [r.status_code for r in responses] == [202] * 3
        assert len({r.text for r in responses}) == 1

    async def test_send_failure_leaves_no_open_request(self, fresh_db, anon_client, mailer):
        await _register(anon_client)
        mailer.side_effect = OSError("relay down")
        resp = await anon_client.post("/auth/forgot-password", json={"email": "owner@acme.com"})
        assert resp.status_code == 202
        assert await fresh_db.fetchval("SELECT count(*) FROM password_reset_requests") == 0

    async def test_code_is_stored_hashed(self, fresh_db, anon_client, mailer):
        await _register(anon_client)
        code = await self._request_code(anon_client, mailer)
        assert code not in await fresh_db.fetchval("SELECT code_hash FROM password_reset_requests")


class TestSessionRevocation:
    async def _login(self, client, password: str = "a-real-password") -> str:
        resp = await client.post("/auth/login", json={"email": "owner@acme.com", "password": password})
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    async def _works(self, client, token: str) -> bool:
        me = await client.get("/auth/me", headers=_bearer(token))
        users = await client.get("/users", headers=_bearer(token))
        assert me.status_code == users.status_code, (me.text, users.text)
        return me.status_code == 200

    async def test_reset_signs_out_existing_sessions(self, anon_client, mailer):
        await _register(anon_client)
        old = await self._login(anon_client)
        assert await self._works(anon_client, old)
        mailer.reset_mock()
        await anon_client.post("/auth/forgot-password", json={"email": "owner@acme.com"})
        resp = await anon_client.post(
            "/auth/reset-password",
            json={"email": "owner@acme.com", "code": _code(mailer), "new_password": "brand-new-password"},
        )
        assert not await self._works(anon_client, old)
        assert await self._works(anon_client, resp.json()["access_token"])

    async def test_password_change_signs_out_other_sessions_but_not_this_one(self, anon_client):
        await _register(anon_client)
        this, other = await self._login(anon_client), await self._login(anon_client)
        assert await self._works(anon_client, other)
        resp = await anon_client.post(
            "/auth/change-password",
            json={"current_password": "a-real-password", "new_password": "brand-new-password"},
            headers=_bearer(this),
        )
        assert resp.status_code == 200
        assert not await self._works(anon_client, other)
        assert not await self._works(anon_client, this)
        assert await self._works(anon_client, resp.json()["access_token"])

    async def test_revoked_token_cannot_change_email(self, anon_client):
        await _register(anon_client)
        old = await self._login(anon_client)
        await anon_client.post(
            "/auth/change-password",
            json={"current_password": "a-real-password", "new_password": "brand-new-password"},
            headers=_bearer(old),
        )
        resp = await anon_client.post(
            "/auth/change-email",
            json={"current_password": "brand-new-password", "new_email": "attacker@evil.com"},
            headers=_bearer(old),
        )
        assert resp.status_code == 401

    async def test_superadmin_setting_a_password_signs_the_user_out(self, anon_client):
        await users_service.seed_superadmin(**SUPERADMIN)
        root = (await anon_client.post("/auth/login", json=SUPERADMIN)).json()["access_token"]
        admin = await _register(anon_client)
        assert await self._works(anon_client, admin["access_token"])
        resp = await anon_client.patch(
            f"/users/{admin['user']['id']}", json={"password": "brand-new-password"}, headers=_bearer(root),
        )
        assert resp.status_code == 200
        assert not await self._works(anon_client, admin["access_token"])
        assert await self._works(anon_client, await self._login(anon_client, "brand-new-password"))


class TestLogin:
    async def test_still_hides_whether_an_email_exists(self, anon_client):
        await _register(anon_client)
        wrong_password = await anon_client.post(
            "/auth/login", json={"email": "owner@acme.com", "password": "wrong-password"},
        )
        unknown_email = await anon_client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"},
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json()["detail"] == unknown_email.json()["detail"]
