# Knowledge connectors and scheduled re-sync: deferred

Phase 7 (#89) listed "Connectors and scheduled re-sync (CRM, Drive, Confluence)". It is **not built**. It needs
product decisions and credentials this phase did not have, and nothing else in Phase 7 depends on it. This note
records what it will need so the item is not silently dropped.

## What is needed

- **Sources.** A source adapter per system: CRM (which objects: accounts, notes, articles?), Google Drive (files and
  Google Docs export), Confluence (spaces and pages). Each yields `(external_id, title, bytes, content_type,
  modified_at)`; everything after that reuses the existing upload path and parser ladder.
- **Auth.** OAuth through `services/toolexec/oauth.py` (the connection model the custom-API tools already use), per
  tenant, least-privilege read scopes, tokens never in `kb_documents` rows or logs. A tenant disconnecting must stop
  syncs and be able to purge what was imported.
- **Scheduling.** A scheduled re-sync that enqueues work into the existing ingestion queue (`kb_ingestion_jobs`), not
  a second pipeline, so leases, retries, dead-lettering and the per-tenant running cap apply unchanged. A
  per-source cursor (modified-since or change token) and a deletion rule (source file removed means document
  soft-deleted, not hard-deleted).
- **Dedupe.** Skip an unchanged file before it reaches the queue. `kb_documents.content_hash` exists in the schema
  but no code populates or compares it today, so this has to be built: store the hash on ingest, compare the
  source's own hash or modified time first, so an unchanged corpus costs one listing call, not a download.
- **Quotas.** Per-tenant limits on connected sources, documents per source, bytes per sync and syncs per day, so a
  large Drive cannot starve the queue or run up embedding cost. Over-quota is a typed, visible state, like
  every other ingestion outcome.
- **Privacy.** A connector reads tenant data from the tenant's own accounts into that tenant's KB. No data crosses
  tenants, and the sync account is the tenant's own OAuth grant.
- **Tenant isolation.** Any new table (sources, cursors, sync runs) needs an RLS policy in `database/rls.sql` in the
  same change and a case in `tests/test_rls_coverage.py`.

## Not decided

Which systems come first, whether sync is pull-only or webhook-assisted, and where the schedule runs (a worker loop
beside the ingestion worker is the obvious home).
