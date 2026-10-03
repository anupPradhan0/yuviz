"""
reencrypt_tenant_refs against a SEEDED scratch database. An empty database
cannot exercise any of it: the guard, the grouping and the zero-row abort only
fire on live legacy rows, so every test here starts from a database built the
way production looks before this release (legacy refs present, the CHECK that
refuses new ones still NOT VALID).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import uuid
import warnings
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet

from libs.config_sdk.secrets import decrypt_secret, decrypt_tenant_secret, encrypt_secret
from services.did.provider_manager import DidProviderManager
from services.did.secret_resolver import CompositeSecretResolver
from services.telephony.accounts import AccountStore
from services.toolexec import custom_apis, reencrypt_tenant_refs as script

REPO = Path(__file__).resolve().parents[3]
_BASE = os.environ["POSTGRES_DSN"].rsplit("/", 1)[0]
_CONSTRAINT = "custom_apis_auth_config_enc_bound"


def _psql(database: str, *args: str, timeout: float | None = None) -> None:
    subprocess.run(
        ["psql", f"{_BASE}/{database}", "-q", "-v", "ON_ERROR_STOP=1", *args],
        check=True, capture_output=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def scratch_db():
    """A throwaway database built from schema.sql, so the real one is never
    seeded with legacy refs. One per module, reset by `dsn` between tests: a
    database-per-test needs a DROP DATABASE each, which waits on every other
    session's backend."""
    name = f"reencrypt_scratch_{uuid.uuid4().hex[:8]}"
    _psql("postgres", "-c", f"CREATE DATABASE {name}")
    _psql(name, "-f", str(REPO / "database" / "schema.sql"))
    for extra in ("knowledge_schema.sql", "telephony_schema.sql"):
        subprocess.run(["psql", f"{_BASE}/{name}", "-q", "-f", str(REPO / "database" / extra)], check=True, capture_output=True)
    yield name
    try:
        _psql("postgres", "-c", f"DROP DATABASE IF EXISTS {name}", timeout=5)
    except subprocess.TimeoutExpired:
        warnings.warn(f"scratch database {name} was not dropped: DROP DATABASE did not return in 5s")


@pytest.fixture
async def dsn(scratch_db, monkeypatch):
    url = f"{_BASE}/{scratch_db}"
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("TRUNCATE tenants, audit_log, users CASCADE")
        await conn.execute("ALTER TABLE provider_configs ALTER COLUMN tenant_id SET NOT NULL")
    finally:
        await conn.close()
    monkeypatch.setenv("POSTGRES_DSN", url)
    return url


def _undecryptable() -> str:
    return "enc:" + Fernet(Fernet.generate_key()).encrypt(b"other-install").decode()


async def _tenant(conn, slug: str) -> uuid.UUID:
    return await conn.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, f"{slug}-{uuid.uuid4().hex[:6]}")


async def _custom_api(conn, tenant, config: dict, *, scheme="bearer", deleted=False) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method, auth_scheme, auth_config, deleted_at) "
        "VALUES ($1, $2, 'd', 'https://example.com', 'GET', $3, $4::jsonb, CASE WHEN $5 THEN now() END) RETURNING id",
        tenant, f"api-{uuid.uuid4().hex[:8]}", scheme, json.dumps(config), deleted,
    )


async def _telephony(conn, tenant, provider: str, credentials: dict) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        tenant, f"tel-{uuid.uuid4().hex[:8]}", provider, json.dumps(credentials),
    )


async def _carrier(conn, tenant, ref: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO carriers (tenant_id, name, provider, auth_id, auth_token_ref) VALUES ($1, $2, 'twilio', 'AC1', $3) RETURNING id",
        tenant, f"car-{uuid.uuid4().hex[:8]}", ref,
    )


async def _provider_cfg(conn, tenant, ref: str | None) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) VALUES ($1, $2, 'tts', 'e', $3) RETURNING id",
        tenant, f"pc-{uuid.uuid4().hex[:8]}", ref,
    )


async def _tool_provider(conn, tenant, ref: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine, api_key_ref) VALUES ($1, $2, 't', 'e', $3) RETURNING id",
        tenant, f"tp-{uuid.uuid4().hex[:8]}", ref,
    )


_HEALTHY = 200


async def seed(url: str) -> dict:
    """Every case from the design, as live pre-release data."""
    conn = await asyncpg.connect(url)
    try:
        # The pre-release database has no enforced CHECK yet: legacy refs exist.
        await conn.execute(f"ALTER TABLE custom_apis DROP CONSTRAINT {_CONSTRAINT}")
        a, b = await _tenant(conn, "a"), await _tenant(conn, "b")
        s = {"a": a, "b": b, "plain": {}}

        def legacy(name: str) -> str:
            s["plain"][name] = f"plaintext-{name}"
            return encrypt_secret(s["plain"][name])

        # unique A refs: live, soft-deleted, and a row with two legacy fields
        s["u_live_ref"], s["u_del_ref"] = legacy("u_live"), legacy("u_del")
        s["u_live"] = await _custom_api(conn, a, {"token_ref": s["u_live_ref"]})
        s["u_del"] = await _custom_api(conn, a, {"token_ref": s["u_del_ref"]}, deleted=True)
        s["u_cid_ref"], s["u_csec_ref"] = legacy("u_cid"), legacy("u_csec")
        s["u_two"] = await _custom_api(
            conn, a, {"client_id_ref": s["u_cid_ref"], "client_secret_ref": s["u_csec_ref"], "token_url": "https://t"},
            scheme="oauth2_client_credentials",
        )
        # an A/B shared ref
        s["shared_ref"] = legacy("shared")
        s["shared_a"] = await _custom_api(conn, a, {"token_ref": s["shared_ref"]})
        s["shared_b"] = await _custom_api(conn, b, {"token_ref": s["shared_ref"]})
        # an undecryptable ref
        s["bad_ref"] = _undecryptable()
        s["bad"] = await _custom_api(conn, a, {"token_ref": s["bad_ref"]})
        # A's telephony and tool-provider refs copied into B custom_apis
        s["tel_copy_ref"], s["tp_copy_ref"] = legacy("tel_copy"), legacy("tp_copy")
        s["tel_copy_a"] = await _telephony(conn, a, "vobiz", {"auth_id": "x", "auth_token": s["tel_copy_ref"]})
        s["tp_copy_a"] = await _tool_provider(conn, a, s["tp_copy_ref"])
        s["tel_copy_b"] = await _custom_api(conn, b, {"token_ref": s["tel_copy_ref"]})
        s["tp_copy_b"] = await _custom_api(conn, b, {"token_ref": s["tp_copy_ref"]})
        # the telephony pair with no custom_apis occurrence, one entry inside a list
        s["tel_pair_ref"], s["tel_list_shared"], s["tel_list_own"] = legacy("tel_pair"), legacy("tel_ls"), legacy("tel_lo")
        s["tel_pair_a"] = await _telephony(conn, a, "vobiz", {"auth_id": "x", "auth_token": s["tel_pair_ref"]})
        s["tel_pair_b"] = await _telephony(conn, b, "vobiz", {"auth_id": "y", "auth_token": s["tel_pair_ref"]})
        s["tel_list_a"] = await _telephony(conn, a, "cloudonix", {"api_keys": [s["tel_list_shared"], s["tel_list_own"]]})
        s["tel_list_b"] = await _telephony(conn, b, "cloudonix", {"api_keys": [s["tel_list_shared"]]})
        # both carrier cases
        s["car_pair_ref"], s["car_copy_ref"] = legacy("car_pair"), legacy("car_copy")
        s["car_pair_a"], s["car_pair_b"] = await _carrier(conn, a, s["car_pair_ref"]), await _carrier(conn, b, s["car_pair_ref"])
        s["car_copy_a"] = await _carrier(conn, a, s["car_copy_ref"])
        s["car_copy_b"] = await _custom_api(conn, b, {"token_ref": s["car_copy_ref"]})
        # pointers: an env: ref in custom_apis, and one in Config that must be left alone
        s["env_api"] = await _custom_api(conn, a, {"token_ref": "env:OPENAI_API_KEY"})
        s["env_cfg"] = await _provider_cfg(conn, a, "env:OPENAI_API_KEY")
        # a single-tenant Config ref that must survive byte-identical
        s["single_ref"] = legacy("single")
        s["single"] = await _provider_cfg(conn, a, s["single_ref"])
        # A production-shaped population of healthy credentials around the
        # cases above: the ceiling is a proportion, and a database made only of
        # attack cases is not a normal run.
        s["healthy"] = [await _provider_cfg(conn, a if i % 2 else b, legacy(f"healthy{i}")) for i in range(_HEALTHY)]

        await conn.execute(
            f"ALTER TABLE custom_apis ADD CONSTRAINT {_CONSTRAINT} CHECK (auth_config::text !~ '\"enc:(?!t1\\.)') NOT VALID"
        )
        return s
    finally:
        await conn.close()


async def seed_platform_copy(url: str, s: dict) -> None:
    """A ciphertext on a platform provider_configs row (tenant_id NULL) copied
    into A. provider_configs.tenant_id is NOT NULL in schema.sql today, so the
    scratch database relaxes it to reach the case the design names."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("ALTER TABLE provider_configs ALTER COLUMN tenant_id DROP NOT NULL")
        await conn.execute(f"ALTER TABLE custom_apis DROP CONSTRAINT {_CONSTRAINT}")
        s["platform_ref"] = encrypt_secret("platform-key")
        s["platform"] = await _provider_cfg(conn, None, s["platform_ref"])
        s["platform_copy"] = await _custom_api(conn, s["a"], {"token_ref": s["platform_ref"]})
        await conn.execute(
            f"ALTER TABLE custom_apis ADD CONSTRAINT {_CONSTRAINT} CHECK (auth_config::text !~ '\"enc:(?!t1\\.)') NOT VALID"
        )
    finally:
        await conn.close()


_SNAPSHOT_SQL = (
    "SELECT 'custom_apis', id::text, auth_config::text FROM custom_apis UNION ALL "
    + " UNION ALL ".join(
        f"SELECT '{t}', id::text, {c}::text FROM {t}" for t, c, _ in script._CONFIG_REF_COLUMNS
    )
    + " ORDER BY 1, 2"
)


async def snapshot(url: str) -> list:
    conn = await asyncpg.connect(url)
    try:
        return [tuple(r) for r in await conn.fetch(_SNAPSHOT_SQL)]
    finally:
        await conn.close()


async def fetch_one(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def stored(url: str, table: str, row_id) -> dict | str:
    column = {"custom_apis": "auth_config", "telephony_configs": "credentials"}.get(table)
    if column:
        return json.loads(await fetch_one(url, f"SELECT {column}::text FROM {table} WHERE id = $1", row_id))
    column = dict((t, c) for t, c, _ in script._CONFIG_REF_COLUMNS)[table]
    return await fetch_one(url, f"SELECT {column} FROM {table} WHERE id = $1", row_id)


async def audit_reasons(url: str, row_id, tenant) -> list[str]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            "SELECT new_value->>'reason' AS reason FROM audit_log WHERE entity_id = $1 AND tenant_id = $2", row_id, tenant,
        )
        return [r["reason"] for r in rows]
    finally:
        await conn.close()


def _main(argv: list[str]) -> int:
    try:
        script.main(argv)
    except SystemExit as exit_:
        return exit_.code
    return 0


async def run_main(argv: list[str]) -> int:
    """main() owns its event loop, so it runs off the test's loop."""
    return await asyncio.to_thread(_main, argv)


Q = "quarantined"
SHARED, UNDEC, POINTER = script._REASON_SHARED, script._REASON_UNDECRYPTABLE, script._REASON_POINTER


def test_the_script_and_the_masking_agree_on_the_quarantine_marker():
    assert script.QUARANTINED == custom_apis.QUARANTINED == "quarantined"


async def test_unique_refs_are_rebound_to_their_own_tenant(dsn):
    s = await seed(dsn)
    counts, _ = await script.reencrypt(dsn)

    live = (await stored(dsn, "custom_apis", s["u_live"]))["token_ref"]
    deleted = (await stored(dsn, "custom_apis", s["u_del"]))["token_ref"]
    two = await stored(dsn, "custom_apis", s["u_two"])
    for ref, name in ((live, "u_live"), (deleted, "u_del"), (two["client_id_ref"], "u_cid"), (two["client_secret_ref"], "u_csec")):
        assert ref.startswith("enc:t1.")
        assert decrypt_tenant_secret(s["a"], ref) == s["plain"][name]
        with pytest.raises(ValueError):
            decrypt_tenant_secret(s["b"], ref)
    assert two["token_url"] == "https://t"
    assert counts["rebound"] == 4


async def test_every_occurrence_of_a_shared_ciphertext_is_quarantined_with_audit_and_report(dsn):
    s = await seed(dsn)
    counts, report = await script.reencrypt(dsn)

    assert (await stored(dsn, "custom_apis", s["shared_a"]))["token_ref"] == Q
    assert (await stored(dsn, "custom_apis", s["shared_b"]))["token_ref"] == Q
    for key in ("tel_copy_b", "tp_copy_b", "car_copy_b"):
        assert (await stored(dsn, "custom_apis", s[key]))["token_ref"] == Q
    assert (await stored(dsn, "telephony_configs", s["tel_copy_a"]))["auth_token"] == Q
    assert await stored(dsn, "tool_provider_configs", s["tp_copy_a"]) == Q
    assert await stored(dsn, "carriers", s["car_copy_a"]) == Q
    for key in ("tel_pair_a", "tel_pair_b"):
        assert (await stored(dsn, "telephony_configs", s[key]))["auth_token"] == Q
    for key in ("car_pair_a", "car_pair_b"):
        assert await stored(dsn, "carriers", s[key]) == Q
    # A list entry that is shared goes; the same row's own entry stays, byte for byte.
    assert (await stored(dsn, "telephony_configs", s["tel_list_a"]))["api_keys"] == [Q, s["tel_list_own"]]
    assert (await stored(dsn, "telephony_configs", s["tel_list_b"]))["api_keys"] == [Q]

    # An audit row on each occurrence's own tenant, and a report line for it.
    assert await audit_reasons(dsn, s["shared_a"], s["a"]) == [SHARED]
    assert await audit_reasons(dsn, s["shared_b"], s["b"]) == [SHARED]
    assert await audit_reasons(dsn, s["car_copy_a"], s["a"]) == [SHARED]
    assert await audit_reasons(dsn, s["tel_pair_b"], s["b"]) == [SHARED]
    reported = {(r["table"], r["row_id"]) for r in report if r["reason"] == SHARED}
    assert ("carriers", str(s["car_pair_a"])) in reported and ("telephony_configs", str(s["tel_pair_b"])) in reported
    assert all(set(r) == {"table", "column", "row_id", "tenant_id", "reason"} for r in report)
    assert counts["config_shared"] == 0 and counts["remaining"] == 0


async def test_an_undecryptable_ref_is_quarantined(dsn):
    s = await seed(dsn)
    await script.reencrypt(dsn)
    assert (await stored(dsn, "custom_apis", s["bad"]))["token_ref"] == Q
    assert await audit_reasons(dsn, s["bad"], s["a"]) == [UNDEC]


async def test_a_platform_copied_ref_is_quarantined_everywhere_including_the_platform_row(dsn):
    s = await seed(dsn)
    await seed_platform_copy(dsn, s)
    _, report = await script.reencrypt(dsn, allow_large_quarantine=True)
    assert (await stored(dsn, "custom_apis", s["platform_copy"]))["token_ref"] == Q
    assert await stored(dsn, "provider_configs", s["platform"]) == Q
    # A platform row has no tenant audit log; it is in the report only.
    platform_lines = [r for r in report if r["row_id"] == str(s["platform"])]
    assert platform_lines == [{
        "table": "provider_configs", "column": "api_key_ref", "row_id": str(s["platform"]),
        "tenant_id": None, "reason": SHARED,
    }]
    assert await fetch_one(dsn, "SELECT count(*) FROM audit_log WHERE entity_id = $1", s["platform"]) == 0


async def test_pointers_in_custom_apis_are_quarantined_and_config_pointers_are_left_alone(dsn):
    s = await seed(dsn)
    counts, _ = await script.reencrypt(dsn)
    assert (await stored(dsn, "custom_apis", s["env_api"]))["token_ref"] == Q
    assert await audit_reasons(dsn, s["env_api"], s["a"]) == [POINTER]
    assert counts["quarantined_pointer_refs"] == 1
    assert await stored(dsn, "provider_configs", s["env_cfg"]) == "env:OPENAI_API_KEY"


async def test_a_single_tenant_config_ref_survives_byte_identical(dsn):
    s = await seed(dsn)
    await script.reencrypt(dsn)
    assert await stored(dsn, "provider_configs", s["single"]) == s["single_ref"]
    assert decrypt_secret(s["single_ref"]) == s["plain"]["single"]
    for i, row_id in enumerate(s["healthy"]):
        assert decrypt_secret(await stored(dsn, "provider_configs", row_id)) == s["plain"][f"healthy{i}"]


async def test_counts_validation_and_idempotence(dsn, tmp_path, capsys):
    s = await seed(dsn)
    assert await fetch_one(dsn, f"SELECT convalidated FROM pg_constraint WHERE conname = '{_CONSTRAINT}'") is False

    counts, _ = await script.reencrypt(dsn)
    assert counts["quarantined_config"] == {
        "provider_configs": 0, "tool_provider_configs": 1, "telephony_configs": 5, "carriers": 3,
    }
    assert counts["quarantined_custom_apis"] == 6  # shared_a/b, tel_copy_b, tp_copy_b, car_copy_b, bad
    assert await fetch_one(dsn, f"SELECT convalidated FROM pg_constraint WHERE conname = '{_CONSTRAINT}'") is True

    after_first = await snapshot(dsn)
    audit_rows = await fetch_one(dsn, "SELECT count(*) FROM audit_log")
    report = tmp_path / "second.jsonl"
    assert await run_main(["--report", str(report)]) == 0
    assert await snapshot(dsn) == after_first
    assert await fetch_one(dsn, "SELECT count(*) FROM audit_log") == audit_rows
    assert json.loads(capsys.readouterr().out) == {
        "config_shared": 0, "quarantined_config": {t: 0 for t, _, _ in script._CONFIG_REF_COLUMNS},
        "quarantined_custom_apis": 0, "quarantined_pointer_refs": 0, "rebound": 0, "remaining": 0,
    }
    assert report.read_text() == ""
    assert s  # the seed was live


async def test_report_is_0600_ids_only_and_written_after_commit(dsn, tmp_path):
    s = await seed(dsn)
    report = tmp_path / "report.jsonl"
    assert await run_main(["--report", str(report)]) == 0
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    text = report.read_text()
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines and all(set(line) == {"table", "column", "row_id", "tenant_id", "reason"} for line in lines)
    for secret in (s["shared_ref"], s["tel_pair_ref"], s["car_pair_ref"], s["bad_ref"]):
        assert secret not in text


async def test_dry_run_changes_nothing_and_writes_no_report(dsn, tmp_path):
    await seed(dsn)
    before = await snapshot(dsn)
    audit_rows = await fetch_one(dsn, "SELECT count(*) FROM audit_log")
    counts, _ = await script.reencrypt(dsn, dry_run=True)
    report = tmp_path / "dry.jsonl"
    assert await run_main(["--dry-run", "--report", str(report)]) == 0
    assert counts["rebound"] == 4 and counts["remaining"] == 0
    assert await snapshot(dsn) == before
    assert await fetch_one(dsn, "SELECT count(*) FROM audit_log") == audit_rows
    assert await fetch_one(dsn, f"SELECT convalidated FROM pg_constraint WHERE conname = '{_CONSTRAINT}'") is False
    assert not report.exists()


# `_ref` columns that never hold a legacy credential. Naming one here is a
# decision a reviewer sees; a new `_ref` column that is in neither this set nor
# _CONFIG_REF_COLUMNS fails the test below.
_NOT_LEGACY_REFS = {
    ("calls", "recording_ref"),              # storage locator of a recording
    ("carriers", "carrier_account_ref"),     # the carrier's own account id
    ("kb_documents", "source_ref"),          # storage locator of a document
    # Created tenant-bound: their CHECKs refuse anything but enc:t1. from the first insert.
    ("oauth_connections", "access_token_ref"),
    ("oauth_connections", "refresh_token_ref"),
    ("oauth_authorization_states", "code_verifier_ref"),
}


async def test_the_inventory_matches_information_schema(dsn):
    """Computed from the database, not a literal (lessons 29, 42): a ref
    column added to schema.sql and classified nowhere fails here."""
    conn = await asyncpg.connect(dsn)
    try:
        found = {
            (r["table_name"], r["column_name"]) for r in await conn.fetch(
                "SELECT c.table_name, c.column_name FROM information_schema.columns c "
                "JOIN information_schema.tables t USING (table_schema, table_name) "
                "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                "AND c.table_name IN (SELECT table_name FROM information_schema.columns "
                "                     WHERE table_schema = 'public' AND column_name = 'tenant_id') "
                "AND ((c.data_type = 'text' AND c.column_name LIKE '%\\_ref' ESCAPE '\\') "
                "     OR (c.data_type = 'jsonb' AND c.column_name = 'credentials')) "
                "AND c.table_name <> 'custom_apis'"
            )
        }
    finally:
        await conn.close()
    declared = {(t, c) for t, c, _ in script._CONFIG_REF_COLUMNS}
    assert found, "the information_schema query matched nothing"
    assert _NOT_LEGACY_REFS <= found, "a column named as never holding a legacy ref no longer exists"
    assert declared == found - _NOT_LEGACY_REFS
    assert len(script._CONFIG_REF_COLUMNS) == len(found - _NOT_LEGACY_REFS)


async def test_removing_a_table_from_the_inventory_rebinds_a_copy_to_the_copier(dsn, monkeypatch):
    """The mutation the inventory tripwire exists for: with telephony and
    tool-provider dropped from _CONFIG_REF_COLUMNS the script sees only B's
    custom_apis copy, treats it as B's own, and seals it to B."""
    s = await seed(dsn)
    monkeypatch.setattr(script, "_CONFIG_REF_COLUMNS", tuple(
        c for c in script._CONFIG_REF_COLUMNS if c[0] not in ("telephony_configs", "tool_provider_configs")
    ))
    await script.reencrypt(dsn)
    for key in ("tel_copy_b", "tp_copy_b"):
        assert (await stored(dsn, "custom_apis", s[key]))["token_ref"].startswith("enc:t1.")


async def test_without_config_quarantine_writes_the_gate_fails(dsn, monkeypatch, capsys):
    """The gate cannot pass vacuously: with Config quarantine disabled the
    shared Config ciphertexts stay live, config_shared says so, exit is 1."""
    async def _no_write(*args, **kwargs):
        return None

    monkeypatch.setattr(script, "_replace_scalar", _no_write)
    monkeypatch.setattr(script, "_replace_credentials", _no_write)
    await seed(dsn)
    assert await run_main([]) == 1
    assert json.loads(capsys.readouterr().out)["config_shared"] != 0


async def test_a_skipped_row_rolls_back_all_five_columns_and_writes_no_report(dsn, monkeypatch, tmp_path):
    await seed(dsn)
    before = await snapshot(dsn)
    real = script._replace_custom_api
    skipped = []

    async def _skip_one(conn, row_id, *rest):
        if not skipped and (await conn.fetchval("SELECT auth_config ? 'token_ref' FROM custom_apis WHERE id = $1", row_id)):
            skipped.append(row_id)
            return
        await real(conn, row_id, *rest)

    monkeypatch.setattr(script, "_replace_custom_api", _skip_one)
    report = tmp_path / "never.jsonl"
    assert await run_main(["--report", str(report)]) == 1
    assert skipped
    assert await snapshot(dsn) == before
    assert not report.exists()
    assert await fetch_one(dsn, "SELECT count(*) FROM audit_log") == 0
    assert await fetch_one(dsn, f"SELECT convalidated FROM pg_constraint WHERE conname = '{_CONSTRAINT}'") is False


async def test_a_missing_key_stops_the_run_without_touching_the_database(dsn, monkeypatch):
    await seed(dsn)
    before = await snapshot(dsn)
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY")
    with pytest.raises(Exception, match="SECRET_ENCRYPTION_KEY"):
        await script.reencrypt(dsn)
    assert await snapshot(dsn) == before


# ── the readers fail closed on what the script wrote ─────────────────────

async def test_did_provider_manager_refuses_a_quarantined_carrier_and_would_not_refuse_null(dsn):
    """provider_manager.get() passes auth_token=None through when the ref is
    falsy. With a factory that tolerates a missing token (the registry is
    pluggable), NULL would build a provider; "quarantined" must raise."""
    s = await seed(dsn)
    await script.reencrypt(dsn)

    async def _tolerant(carrier, auth_token):
        return ("provider", auth_token)

    quarantined = await stored(dsn, "carriers", s["car_pair_a"])
    manager = DidProviderManager(CompositeSecretResolver(), registry={"twilio": _tolerant})
    with pytest.raises(ValueError):
        await manager.get({"id": "c1", "provider": "twilio", "auth_token_ref": quarantined})
    assert await DidProviderManager(CompositeSecretResolver(), registry={"twilio": _tolerant}).get(
        {"id": "c2", "provider": "twilio", "auth_token_ref": None}
    ) == ("provider", None)


def test_telephony_account_store_refuses_a_quarantined_credential():
    """The design said decrypt_secret raises here. It does not: _decrypt_scalar
    only reaches decrypt_secret for enc: values and passed anything else
    through, so the literal "quarantined" went to the provider AS the
    credential — the one reader where quarantine did not fail closed.

    It now skips instead of raising, which is what the surrounding loop
    already does for an undecryptable field; raising would take out every
    other account in the same refresh. What matters is only that the sentinel
    never comes back out as a usable credential."""
    assert AccountStore()._decrypt_field("acct", Q) is None
    assert AccountStore()._decrypt_field("acct", [Q, Q]) == []


# ── the blast-radius ceiling ─────────────────────────────────────────────

async def _plant_copies(url: str, s: dict, count: int) -> None:
    """The viewer scenario: copies of tenant A's own live ciphertext written
    into B's rows, so that quarantining every occurrence would take A's
    credential offline."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(f"ALTER TABLE custom_apis DROP CONSTRAINT {_CONSTRAINT}")
        for _ in range(count):
            await _custom_api(conn, s["b"], {"token_ref": s["u_live_ref"]})
        await conn.execute(
            f"ALTER TABLE custom_apis ADD CONSTRAINT {_CONSTRAINT} CHECK (auth_config::text !~ '\"enc:(?!t1\\.)') NOT VALID"
        )
    finally:
        await conn.close()


async def _validated(url: str) -> bool:
    return await fetch_one(url, f"SELECT convalidated FROM pg_constraint WHERE conname = '{_CONSTRAINT}'")


async def test_planted_copies_abort_with_exit_2_and_change_nothing(dsn, capsys):
    s = await seed(dsn)
    await _plant_copies(dsn, s, 40)
    before = await snapshot(dsn)

    assert await run_main([]) == 2
    assert await snapshot(dsn) == before
    assert await _validated(dsn) is False
    assert await fetch_one(dsn, "SELECT count(*) FROM audit_log") == 0
    err = capsys.readouterr().err
    assert "tripped: rows" in err
    assert not any(str(v) in err for v in (s["a"], s["b"], s["u_live"]))  # counts only


async def test_the_override_flag_proceeds_past_the_ceiling(dsn, caplog):
    s = await seed(dsn)
    await _plant_copies(dsn, s, 40)

    with caplog.at_level("WARNING", logger=script.__name__):
        assert await run_main(["--allow-large-quarantine"]) == 0
    assert (await stored(dsn, "custom_apis", s["u_live"]))["token_ref"] == Q
    assert await _validated(dsn) is True
    assert any("ceiling overridden" in r.getMessage() and "tripped: rows" in r.getMessage() for r in caplog.records)


async def test_dry_run_reports_that_the_ceiling_would_trip(dsn):
    s = await seed(dsn)
    await _plant_copies(dsn, s, 40)
    before = await snapshot(dsn)
    assert await run_main(["--dry-run"]) == 2
    assert await snapshot(dsn) == before
    assert await run_main(["--dry-run", "--allow-large-quarantine"]) == 0


async def test_a_wrong_encryption_key_aborts_on_the_undecryptable_floor(dsn, monkeypatch, capsys):
    """Every ciphertext fails to decrypt under the wrong key. Count and
    percentage limits are lifted so only the floor is left to stop it."""
    await seed(dsn)
    before = await snapshot(dsn)
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())

    assert await run_main(["--max-quarantine", "100000", "--max-quarantine-pct", "100"]) == 2
    assert await snapshot(dsn) == before
    assert "tripped: undecryptable" in capsys.readouterr().err
    assert await _validated(dsn) is False


async def test_a_platform_row_in_the_plan_aborts_unless_overridden(dsn):
    s = await seed(dsn)
    await seed_platform_copy(dsn, s)
    before = await snapshot(dsn)
    assert await run_main([]) == 2
    assert await snapshot(dsn) == before
    assert await run_main(["--allow-large-quarantine"]) == 0
    assert await stored(dsn, "provider_configs", s["platform"]) == Q


async def test_a_normal_small_seed_passes_with_no_flags(dsn):
    await seed(dsn)
    assert await run_main([]) == 0
    assert await _validated(dsn) is True
