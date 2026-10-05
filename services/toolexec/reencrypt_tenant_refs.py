"""
One-shot release step: retire every legacy (unbound Fernet) credential ref
and every tenant-written pointer ref that predates tenant-bound ciphertext.

    python -m services.toolexec.reencrypt_tenant_refs [--dry-run] [--report <path>]

A legacy `enc:` token opens for whoever holds it, so an identical string under
two tenants means one was pasted from the other. Nothing in the database dates
a ciphertext's arrival in a row (created_at dates the row, updated_at moves for
unrelated edits, and the audit log has always redacted these columns), so a
shared ciphertext is quarantined in EVERY occurrence, the original holder
included, rather than guessing who copied whom. A ciphertext held by exactly
one tenant is rebound: decrypted once here and sealed to that tenant, so
toolexec can open it and no other tenant can.

Quarantine writes the literal "quarantined", never NULL. NULL reads as "no
credential configured" and some callers carry on with None; "quarantined" has
no scheme, so every reader raises.

Runs under the superuser DSN (POSTGRES_DSN) in one transaction that starts by
locking the five tables, writes nothing a concurrent request can interleave
with, and ends by validating the CHECK that refuses new legacy refs. Exit 0
means that CHECK is validated and no shared ciphertext is left live; exit 1
means the run failed or left one; exit 2 means the blast-radius ceiling
stopped it before any write. stdout is counts only.

The ceiling exists because quarantine is destructive and its inputs are
attacker- and operator-influenced: a viewer in tenant A can plant copies of
A's own live ciphertext to have A's credentials quarantined, and a wrong
SECRET_ENCRYPTION_KEY makes every ciphertext "undecryptable". Past the
ceiling the run rolls back unless --allow-large-quarantine is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import asyncpg

from libs.config_sdk.secrets import (
    QUARANTINED,
    SecretEncryptionUnavailable,
    decrypt_secret,
    encrypt_tenant_secret,
)

from . import audit

log = logging.getLogger(__name__)

# (table, column, kind). The scan and the quarantine writes both iterate this
# one tuple, and a test compares it with information_schema, so a fifth ref
# column cannot be added to schema.sql and silently skipped here.
_CONFIG_REF_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("provider_configs", "api_key_ref", "scalar"),
    ("tool_provider_configs", "api_key_ref", "scalar"),
    ("telephony_configs", "credentials", "jsonb_credentials"),
    ("carriers", "auth_token_ref", "scalar"),
)
_CUSTOM_API_REF_FIELDS = ("key_ref", "token_ref", "client_id_ref", "client_secret_ref")
_ENTITY_TYPES = {
    "custom_apis": "custom_api",
    "provider_configs": "provider_config",
    "tool_provider_configs": "tool_provider_config",
    "telephony_configs": "telephony_config",
    "carriers": "carrier",
}
_LOCKED_TABLES = ("custom_apis", *(table for table, _, _ in _CONFIG_REF_COLUMNS))

_BOUND_PREFIX = "enc:t1."
_REASON_SHARED = "credential_quarantined_shared_ciphertext"
_REASON_UNDECRYPTABLE = "credential_quarantined_undecryptable"
_REASON_POINTER = "credential_quarantined_pointer_ref"

_CONSTRAINT = "custom_apis_auth_config_enc_bound"

# Blast-radius ceiling defaults (documented in docs/setup.md, T26).
DEFAULT_MAX_QUARANTINE = 25          # rows
DEFAULT_MAX_QUARANTINE_PCT = 10      # of all rows holding a ref
UNDECRYPTABLE_FLOOR = 3              # more than this is the wrong-key signature


@dataclass(frozen=True)
class _Occurrence:
    table: str
    column: str          # as the report names it, e.g. "auth_config.key_ref"
    row_id: uuid.UUID
    tenant_id: uuid.UUID | None
    key: str | None      # custom_apis field, or telephony credentials key
    index: int | None    # position inside a telephony list-valued key
    value: str


@dataclass
class _Scan:
    legacy: list[_Occurrence]
    pointers: list[_Occurrence]
    # (tenant, exact current document) per row: the document is the predicate
    # of the write that replaces it.
    documents: dict[tuple[str, uuid.UUID], tuple[uuid.UUID | None, str]]
    ref_rows: int = 0  # rows holding any ref at all: the ceiling's denominator


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("enc:", "env:", "k8s:"))


def _is_legacy(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("enc:") and not value.startswith(_BOUND_PREFIX)


async def _scan(conn: asyncpg.Connection) -> _Scan:
    scan = _Scan(legacy=[], pointers=[], documents={})

    for row in await conn.fetch("SELECT id, tenant_id, auth_config::text AS doc FROM custom_apis"):
        scan.documents[("custom_apis", row["id"])] = (row["tenant_id"], row["doc"])
        config = json.loads(row["doc"])
        scan.ref_rows += any(_is_ref(config.get(field)) for field in _CUSTOM_API_REF_FIELDS)
        for field in _CUSTOM_API_REF_FIELDS:
            value = config.get(field)
            if not isinstance(value, str):
                continue
            occurrence = _Occurrence(
                "custom_apis", f"auth_config.{field}", row["id"], row["tenant_id"], field, None, value,
            )
            if _is_legacy(value):
                scan.legacy.append(occurrence)
            elif value.startswith(("env:", "k8s:")):
                scan.pointers.append(occurrence)

    for table, column, kind in _CONFIG_REF_COLUMNS:
        if kind == "scalar":
            rows = await conn.fetch(
                f"SELECT id, tenant_id, {column} AS value FROM {table} "
                f"WHERE {column} LIKE 'enc:%' AND {column} NOT LIKE 'enc:t1.%'"
            )
            scan.legacy += [_Occurrence(table, column, r["id"], r["tenant_id"], None, None, r["value"]) for r in rows]
            scan.ref_rows += await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {column} ~ '^(enc|env|k8s):'")
            continue
        # jsonb_credentials: every string value and list entry, whatever the
        # provider registry says its fields are (same walk as
        # public_telephony_config).
        for row in await conn.fetch(f"SELECT id, tenant_id, {column}::text AS doc FROM {table}"):
            scan.documents[(table, row["id"])] = (row["tenant_id"], row["doc"])
            credentials = json.loads(row["doc"])
            scan.ref_rows += any(
                _is_ref(entry) for value in credentials.values() for entry in (value if isinstance(value, list) else [value])
            )
            for key, value in credentials.items():
                entries = enumerate(value) if isinstance(value, list) else [(None, value)]
                for index, entry in entries:
                    if _is_legacy(entry):
                        scan.legacy.append(
                            _Occurrence(table, f"{column}.{key}", row["id"], row["tenant_id"], key, index, entry)
                        )
    return scan


def _classify(scan: _Scan) -> tuple[list[tuple[_Occurrence, str]], list[tuple[_Occurrence, str]]]:
    """Returns (rebinds as (occurrence, new ref), quarantines as (occurrence, reason))."""
    rebinds: list[tuple[_Occurrence, str]] = []
    quarantines: list[tuple[_Occurrence, str]] = []

    groups: dict[str, list[_Occurrence]] = defaultdict(list)
    for occurrence in scan.legacy:
        groups[occurrence.value].append(occurrence)

    for value, occurrences in groups.items():
        owners = {o.tenant_id for o in occurrences}
        if len(owners) > 1 or None in owners:
            quarantines += [(o, _REASON_SHARED) for o in occurrences]
            continue
        try:
            plaintext = decrypt_secret(value)
        except SecretEncryptionUnavailable:
            quarantines += [(o, _REASON_UNDECRYPTABLE) for o in occurrences]
            continue
        (tenant_id,) = owners
        # Config's own readers still open legacy tokens, so only the
        # custom_apis copies move to the tenant-bound form.
        rebinds += [(o, encrypt_tenant_secret(tenant_id, plaintext)) for o in occurrences if o.table == "custom_apis"]

    quarantines += [(o, _REASON_POINTER) for o in scan.pointers]
    return rebinds, quarantines


class QuarantineCeilingExceeded(Exception):
    """The planned quarantine is larger than a legitimate run would produce.
    The message carries counts only."""


def _check_ceiling(
    scan: _Scan, quarantines: list[tuple[_Occurrence, str]], *,
    max_rows: int, max_pct: float, allow_large: bool,
) -> None:
    rows = {(o.table, o.row_id) for o, _ in quarantines}
    undecryptable = sum(1 for _, reason in quarantines if reason == _REASON_UNDECRYPTABLE)
    platform_rows = len({o.row_id for o, _ in quarantines if o.table == "provider_configs" and o.tenant_id is None})

    tripped = []
    if len(rows) > max_rows:
        tripped.append("rows")
    if len(rows) * 100 > max_pct * scan.ref_rows:
        tripped.append("percent")
    if undecryptable > UNDECRYPTABLE_FLOOR:
        tripped.append("undecryptable")
    if platform_rows:
        tripped.append("platform_rows")
    if not tripped:
        return

    summary = (
        f"{len(rows)} of {scan.ref_rows} ref-bearing rows planned, {undecryptable} undecryptable, "
        f"{platform_rows} platform rows; tripped: {', '.join(tripped)}"
    )
    if not allow_large:
        raise QuarantineCeilingExceeded(summary)
    log.warning("quarantine ceiling overridden by --allow-large-quarantine: %s", summary)


def _live_shared(scan: _Scan) -> int:
    owners: dict[str, set[uuid.UUID | None]] = defaultdict(set)
    for occurrence in scan.legacy:
        owners[occurrence.value].add(occurrence.tenant_id)
    return sum(1 for tenants in owners.values() if len(tenants) > 1)


def _executed_one(status: str) -> bool:
    return status == "UPDATE 1"


async def _replace_custom_api(
    conn: asyncpg.Connection, row_id: uuid.UUID, tenant_id: uuid.UUID, old_doc: str, new_doc: dict,
) -> None:
    """One write per row, never one per field: the NOT VALID CHECK is
    re-evaluated on every UPDATE of the row, so a row that still held a legacy
    ref in another field would refuse a half-converted document."""
    status = await conn.execute(
        "UPDATE custom_apis SET auth_config = $4::jsonb WHERE id = $1 AND tenant_id = $2 AND auth_config = $3::jsonb",
        row_id, tenant_id, old_doc, json.dumps(new_doc),
    )
    if not _executed_one(status):
        raise RuntimeError("custom_apis row changed under the lock; aborting")


async def _replace_scalar(
    conn: asyncpg.Connection, table: str, column: str, row_id: uuid.UUID, old: str, new: str,
) -> None:
    status = await conn.execute(f"UPDATE {table} SET {column} = $3 WHERE id = $1 AND {column} = $2", row_id, old, new)
    if not _executed_one(status):
        raise RuntimeError(f"{table} row changed under the lock; aborting")


async def _replace_credentials(
    conn: asyncpg.Connection, table: str, column: str, row_id: uuid.UUID, old_doc: str, new_doc: dict,
) -> None:
    status = await conn.execute(
        f"UPDATE {table} SET {column} = $3::jsonb WHERE id = $1 AND {column} = $2::jsonb",
        row_id, old_doc, json.dumps(new_doc),
    )
    if not _executed_one(status):
        raise RuntimeError(f"{table} row changed under the lock; aborting")


async def _audit_quarantine(conn: asyncpg.Connection, occurrence: _Occurrence, reason: str) -> None:
    # A platform row has no tenant whose audit log could carry the entry; it
    # appears in the report only.
    if occurrence.tenant_id is None:
        return
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(occurrence.tenant_id))
    await audit.write_audit(
        conn, entity_type=_ENTITY_TYPES[occurrence.table], entity_id=occurrence.row_id, action="updated",
        new_value={"reason": reason, "field": occurrence.column},
    )


async def _apply(
    conn: asyncpg.Connection, scan: _Scan,
    rebinds: list[tuple[_Occurrence, str]], quarantines: list[tuple[_Occurrence, str]],
) -> tuple[dict, list[dict]]:
    counts: dict[str, Any] = {
        "rebound": len(rebinds),
        "quarantined_custom_apis": 0,
        "quarantined_pointer_refs": 0,
        "quarantined_config": {table: 0 for table, _, _ in _CONFIG_REF_COLUMNS},
    }
    report: list[dict] = []

    custom_api_fields: dict[uuid.UUID, dict[str, str]] = defaultdict(dict)
    for occurrence, new_ref in rebinds:
        custom_api_fields[occurrence.row_id][occurrence.key] = new_ref

    for occurrence, reason in quarantines:
        if occurrence.table == "custom_apis":
            custom_api_fields[occurrence.row_id][occurrence.key] = QUARANTINED
            counts["quarantined_pointer_refs" if reason == _REASON_POINTER else "quarantined_custom_apis"] += 1
        else:
            counts["quarantined_config"][occurrence.table] += 1
        report.append({
            "table": occurrence.table, "column": occurrence.column, "row_id": str(occurrence.row_id),
            "tenant_id": str(occurrence.tenant_id) if occurrence.tenant_id else None, "reason": reason,
        })
        await _audit_quarantine(conn, occurrence, reason)

    for row_id, fields in custom_api_fields.items():
        tenant_id, old_doc = scan.documents[("custom_apis", row_id)]
        await _replace_custom_api(conn, row_id, tenant_id, old_doc, {**json.loads(old_doc), **fields})

    for table, column, kind in _CONFIG_REF_COLUMNS:
        planned = [o for o, _ in quarantines if o.table == table]
        if kind == "scalar":
            for occurrence in planned:
                await _replace_scalar(conn, table, column, occurrence.row_id, occurrence.value, QUARANTINED)
            continue
        by_row: dict[uuid.UUID, list[_Occurrence]] = defaultdict(list)
        for occurrence in planned:
            by_row[occurrence.row_id].append(occurrence)
        for row_id, occurrences in by_row.items():
            _, old_doc = scan.documents[(table, row_id)]
            new_doc = json.loads(old_doc)
            for occurrence in occurrences:
                if occurrence.index is None:
                    new_doc[occurrence.key] = QUARANTINED
                else:
                    new_doc[occurrence.key][occurrence.index] = QUARANTINED
            await _replace_credentials(conn, table, column, row_id, old_doc, new_doc)

    return counts, report


class _DryRun(Exception):
    """Raised to roll the transaction back after a dry run."""


async def reencrypt(
    dsn: str, *, dry_run: bool = False,
    max_quarantine: int = DEFAULT_MAX_QUARANTINE, max_quarantine_pct: float = DEFAULT_MAX_QUARANTINE_PCT,
    allow_large_quarantine: bool = False,
) -> tuple[dict, list[dict]]:
    """Returns (counts, report rows). The report rows are ids only: never a
    ciphertext, a plaintext or a field value. Raises QuarantineCeilingExceeded
    before any write when the planned quarantine is past the ceiling."""
    # A missing or malformed SECRET_ENCRYPTION_KEY must stop the run, not be
    # mistaken for "every ciphertext fails to decrypt".
    encrypt_tenant_secret(uuid.UUID(int=0), "key-check")

    conn = await asyncpg.connect(dsn)
    try:
        try:
            async with conn.transaction():
                await conn.execute(
                    f"LOCK TABLE {', '.join(_LOCKED_TABLES)} IN SHARE ROW EXCLUSIVE MODE"
                )
                scan = await _scan(conn)
                rebinds, quarantines = _classify(scan)
                _check_ceiling(
                    scan, quarantines, max_rows=max_quarantine, max_pct=max_quarantine_pct,
                    allow_large=allow_large_quarantine,
                )
                counts, report = await _apply(conn, scan, rebinds, quarantines)

                after = await _scan(conn)
                counts["remaining"] = await conn.fetchval(
                    "SELECT count(*) FROM custom_apis WHERE auth_config::text ~ '\"enc:(?!t1\\.)'"
                )
                counts["config_shared"] = _live_shared(after)
                await conn.execute(f"ALTER TABLE custom_apis VALIDATE CONSTRAINT {_CONSTRAINT}")
                if dry_run:
                    raise _DryRun
        except _DryRun:
            pass
    finally:
        await conn.close()
    return counts, report


def _write_report(path: str, report: list[dict]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        for row in report:
            f.write(json.dumps(row) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    parser.add_argument("--report", metavar="PATH", help="write a JSON Lines list of quarantined rows (after commit)")
    parser.add_argument("--max-quarantine", type=int, default=DEFAULT_MAX_QUARANTINE, metavar="N",
                        help=f"abort above N planned rows (default {DEFAULT_MAX_QUARANTINE})")
    parser.add_argument("--max-quarantine-pct", type=float, default=DEFAULT_MAX_QUARANTINE_PCT, metavar="PCT",
                        help=f"abort above PCT%% of ref-bearing rows (default {DEFAULT_MAX_QUARANTINE_PCT})")
    parser.add_argument("--allow-large-quarantine", action="store_true", help="proceed past the ceiling")
    args = parser.parse_args(argv)

    try:
        counts, report = asyncio.run(reencrypt(
            os.environ["POSTGRES_DSN"], dry_run=args.dry_run, max_quarantine=args.max_quarantine,
            max_quarantine_pct=args.max_quarantine_pct, allow_large_quarantine=args.allow_large_quarantine,
        ))
    except QuarantineCeilingExceeded as exc:
        print(f"quarantine ceiling exceeded, nothing was written: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        # Type only: asyncpg's CheckViolation detail carries the failing row.
        print(f"reencrypt_tenant_refs failed and rolled back: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

    if args.report and not args.dry_run:
        _write_report(args.report, report)
    print(json.dumps(counts, sort_keys=True))
    if counts["remaining"] != 0 or counts["config_shared"] != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
