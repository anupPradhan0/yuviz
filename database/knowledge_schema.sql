-- Knowledge platform schema.
-- Run: psql voiceai -f database/knowledge_schema.sql (after database/schema.sql). Idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

-- 'embedding' providers are chosen per knowledge base, not on tenants/agents.
ALTER TABLE provider_configs DROP CONSTRAINT IF EXISTS provider_configs_role_check;
ALTER TABLE provider_configs ADD CONSTRAINT provider_configs_role_check
    CHECK (role IN ('stt', 'llm', 'tts', 'embedding'));

-- ── knowledge_bases ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id),
    slug                  TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    embedding_config_id   UUID REFERENCES provider_configs(id),  -- which embedding provider this KB's chunks were embedded with
    -- inactive = excluded from retrieval, data kept.
    status                TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    config_version        INT NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at            TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

DROP TRIGGER IF EXISTS knowledge_bases_version ON knowledge_bases;
CREATE TRIGGER knowledge_bases_version BEFORE UPDATE ON knowledge_bases
    FOR EACH ROW EXECUTE FUNCTION bump_config_version();

-- ── kb_documents — one row per uploaded source document ─────────────────────
-- source_ref points at StorageProvider-managed content; raw bytes never live in Postgres.
CREATE TABLE IF NOT EXISTS kb_documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),  -- denormalized for cheap tenant-scoped queries/RLS-style checks
    title         TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    content_type  TEXT NOT NULL,  -- e.g. 'application/pdf', 'text/plain', 'text/markdown'
    language      TEXT,
    tags          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- ready = chunks+embeddings exist; failed keeps the doc visible for retry.
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error         TEXT,
    version       INT NOT NULL DEFAULT 1,  -- bumped on re-upload/re-ingest; kb_chunks.version tags which pass produced a chunk
    -- auto: vector retrieval only. prompt: injected into every turn (set by admin, or
    -- automatically for docs under AUTO_INLINE_THRESHOLD_BYTES).
    usage_mode    TEXT NOT NULL DEFAULT 'auto' CHECK (usage_mode IN ('auto', 'prompt')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

-- For databases created before usage_mode existed.
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS usage_mode TEXT NOT NULL DEFAULT 'auto';
DO $$ BEGIN
    ALTER TABLE kb_documents ADD CONSTRAINT kb_documents_usage_mode_check CHECK (usage_mode IN ('auto', 'prompt'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Uploaded file size; NULL for documents uploaded before it was recorded.
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS byte_size BIGINT;

CREATE INDEX IF NOT EXISTS idx_kb_documents_kb ON kb_documents(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant ON kb_documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_status ON kb_documents(status);

-- ── kb_chunks — one row per chunk, with its embedding ────────────────────────
-- 768 dims = nomic-embed-text. Other-dimension models (e.g. 1536) aren't supported yet.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES kb_documents(id),
    kb_id           UUID NOT NULL REFERENCES knowledge_bases(id),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       VECTOR(768),
    token_count     INT,
    page            INT,
    language        TEXT,
    tags            JSONB NOT NULL DEFAULT '{}'::jsonb,
    version         INT NOT NULL DEFAULT 1,  -- = kb_documents.version at the time this chunk was produced
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index, version)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON kb_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_kb ON kb_chunks(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant ON kb_chunks(tenant_id);
-- Cosine ops to match the retrieval query's <=> operator.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- ── agent_knowledge_bases — which KBs an agent retrieves from ───────────────
-- enabled=false pauses a KB for one agent without detaching it.
CREATE TABLE IF NOT EXISTS agent_knowledge_bases (
    agent_id    UUID NOT NULL REFERENCES agents(id),
    kb_id       UUID NOT NULL REFERENCES knowledge_bases(id),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, kb_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_kb_agent ON agent_knowledge_bases(agent_id) WHERE enabled;

-- A KB may only be attached to an agent of its own tenant. The FKs above are tenant-blind and the
-- RLS WITH CHECK tests only the agent, so enforce it here. Runs as the invoking role: a caller who
-- cannot see the KB under RLS finds no row and is refused too (fail closed).
CREATE OR REPLACE FUNCTION agent_knowledge_bases_same_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agents a JOIN knowledge_bases kb ON kb.tenant_id = a.tenant_id
         WHERE a.id = NEW.agent_id AND kb.id = NEW.kb_id
    ) THEN
        RAISE EXCEPTION 'knowledge base % does not belong to the tenant of agent %', NEW.kb_id, NEW.agent_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS agent_knowledge_bases_same_tenant ON agent_knowledge_bases;
CREATE TRIGGER agent_knowledge_bases_same_tenant
    BEFORE INSERT OR UPDATE OF agent_id, kb_id ON agent_knowledge_bases
    FOR EACH ROW EXECUTE FUNCTION agent_knowledge_bases_same_tenant();

-- Rows that predate the trigger are reported, never deleted: retrieval ignores them (tenant
-- predicate on the join) but an operator should review and remove them.
DO $kb_cross_tenant_report$
DECLARE
    bad RECORD;
BEGIN
    FOR bad IN
        SELECT akb.agent_id, akb.kb_id
        FROM agent_knowledge_bases akb
        JOIN agents a ON a.id = akb.agent_id
        JOIN knowledge_bases kb ON kb.id = akb.kb_id
        WHERE kb.tenant_id <> a.tenant_id
    LOOP
        RAISE WARNING 'cross-tenant agent_knowledge_bases row: agent_id=% kb_id=% (review and delete)',
            bad.agent_id, bad.kb_id;
    END LOOP;
END $kb_cross_tenant_report$;

-- ── agent_retrieval_policies — per-agent RetrievalPolicy, never hardcoded ──
-- Precedence: per-call override > this row > system default. No row = system default.
CREATE TABLE IF NOT EXISTS agent_retrieval_policies (
    agent_id            UUID PRIMARY KEY REFERENCES agents(id),
    top_k               INT,
    max_tokens          INT,
    minimum_score       FLOAT,
    rerank              BOOLEAN,       -- Phase 6B extension point — not implemented yet
    hybrid_search       BOOLEAN,       -- Phase 6B extension point — not implemented yet
    include_citations   BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── kb_ingestion_jobs — Postgres-backed job queue, polled by the worker ─────
CREATE TABLE IF NOT EXISTS kb_ingestion_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES kb_documents(id),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    error         TEXT,
    attempts      INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_status ON kb_ingestion_jobs(status) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_document ON kb_ingestion_jobs(document_id);

-- Queue hardening: lease + heartbeat, retry with backoff, dead-letter, error class.
--   pending      = waiting (also a retry waiting for next_attempt_at)
--   running      = claimed; lease_expires_at is pushed forward by the worker heartbeat
--   succeeded    = done
--   failed       = permanent error (retrying cannot help), not retried
--   dead_letter  = transient errors exhausted max_attempts; an admin can retry the document
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS max_attempts      INT NOT NULL DEFAULT 5;
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS stage             TEXT;
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS error_class       TEXT;
-- Background work (a re-ingest after a chunker change): claimed only after every normal job, and never cleared by
-- claim, retry or failure, so a retried re-ingest still queues behind fresh uploads.
ALTER TABLE kb_ingestion_jobs ADD COLUMN IF NOT EXISTS background        BOOLEAN NOT NULL DEFAULT false;
-- Each pair is one DO block so the drop and the re-add commit together.
DO $$ BEGIN
    ALTER TABLE kb_ingestion_jobs DROP CONSTRAINT IF EXISTS kb_ingestion_jobs_status_check;
    ALTER TABLE kb_ingestion_jobs ADD CONSTRAINT kb_ingestion_jobs_status_check
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead_letter'));
END $$;
DO $$ BEGIN
    ALTER TABLE kb_ingestion_jobs DROP CONSTRAINT IF EXISTS kb_ingestion_jobs_error_class_check;
    ALTER TABLE kb_ingestion_jobs ADD CONSTRAINT kb_ingestion_jobs_error_class_check
        CHECK (error_class IS NULL OR error_class IN ('transient', 'permanent'));
END $$;

-- Jobs a pre-lease worker left running have no lease; give them one so the reaper can recover them
-- (a NULL lease never compares as expired, and they would otherwise count against the tenant cap forever).
UPDATE kb_ingestion_jobs SET lease_expires_at = now() + interval '120 seconds'
 WHERE status = 'running' AND lease_expires_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_claim ON kb_ingestion_jobs(next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_lease ON kb_ingestion_jobs(lease_expires_at) WHERE status = 'running';

-- ── kb_ingestion_events — append-only trail: where is my document, and why did it fail ──
CREATE TABLE IF NOT EXISTS kb_ingestion_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       UUID NOT NULL REFERENCES kb_ingestion_jobs(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    attempt      INT NOT NULL,
    stage        TEXT NOT NULL,  -- read | extract | chunk | embed | index | worker
    status       TEXT NOT NULL CHECK (status IN ('ok', 'error', 'retry_scheduled', 'dead_letter', 'lease_expired')),
    duration_ms  INT,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()  -- not now(): events in one transaction must keep insert order
);

CREATE INDEX IF NOT EXISTS idx_kb_ingestion_events_job ON kb_ingestion_events(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kb_ingestion_events_document ON kb_ingestion_events(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kb_ingestion_events_tenant ON kb_ingestion_events(tenant_id);

-- ── Phases 3-6 schema (parsers, chunking, hybrid retrieval, measurement) ────
-- One idempotent block so the parallel phase branches share a single schema change.

-- Documents: how they were parsed, how good the parse was, which chunker produced the chunks.
-- ready_degraded = indexed, but some of the document could not be read (still retrievable).
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS content_hash    TEXT;
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS parser_used     TEXT;
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS quality_score   REAL;
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS chunker_version TEXT;
DO $$ BEGIN
    ALTER TABLE kb_documents DROP CONSTRAINT IF EXISTS kb_documents_status_check;
    ALTER TABLE kb_documents ADD CONSTRAINT kb_documents_status_check
        CHECK (status IN ('pending', 'processing', 'ready', 'ready_degraded', 'failed'));
END $$;

-- Chunks. Parent-child: small child chunks are embedded and searched; the enclosing section is stored as
-- a parent row (is_parent = true, embedding NULL, chunk_index = -1 - parent ordinal so it never collides
-- with a child's index) and returned for context. Lexical search runs on tsv ('simple' config: no
-- language-specific stemming, so it works for every language the platform serves).
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS parent_id       UUID REFERENCES kb_chunks(id) ON DELETE SET NULL;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS is_parent       BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS section_path    TEXT;   -- heading breadcrumb, e.g. 'Refunds > Eligibility'
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS dims            INT;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED;

-- Shadow vector for a re-embed migration (services/knowledge/reembed.py): filled by the migration job with the
-- NEW embedding config while queries keep using `embedding`, then swapped into `embedding` in one transaction.
-- Deliberately no index: it is never searched.
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding_next       VECTOR(768);
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding_next_model TEXT;

CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv ON kb_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_parent ON kb_chunks(parent_id) WHERE parent_id IS NOT NULL;

-- ── retrieval_logs — one row per retrieval: what was asked, what came back, where the time went ──
CREATE TABLE IF NOT EXISTS retrieval_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    agent_id     UUID REFERENCES agents(id),
    trace_id     TEXT NOT NULL,
    query        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'no_match', 'partial', 'unavailable', 'error')),
    degraded     TEXT[] NOT NULL DEFAULT '{}',          -- fallbacks taken, e.g. {lexical_only, rerank_skipped}
    chunk_ids    UUID[] NOT NULL DEFAULT '{}',
    top_score    REAL,
    candidates   INT,
    timings_ms   JSONB NOT NULL DEFAULT '{}'::jsonb,    -- plan, embed, dense, lexical, fuse, rerank, total
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_logs_tenant_time ON retrieval_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_logs_trace ON retrieval_logs(trace_id);
-- The retention purge filters on created_at across every tenant; the index above leads with tenant_id.
CREATE INDEX IF NOT EXISTS idx_retrieval_logs_created ON retrieval_logs(created_at);

-- knowledge_base_ids is default-deny: give the start node the agent's enabled KBs
-- when no node in the graph lists any.
DO $kb_workflow_backfill$
DECLARE
    patched INT := 0;
BEGIN
    WITH agent_kbs AS (
        SELECT
            akb.agent_id,
            jsonb_agg(akb.kb_id::text ORDER BY akb.kb_id) AS kb_ids
        FROM agent_knowledge_bases akb
        JOIN agents ag ON ag.id = akb.agent_id
        JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.tenant_id = ag.tenant_id
        WHERE akb.enabled
        GROUP BY akb.agent_id
    ),
    targets AS (
        SELECT a.id, a.workflow, k.kb_ids,
            (
                SELECT (ord - 1)
                FROM jsonb_array_elements(a.workflow->'nodes') WITH ORDINALITY AS t(n, ord)
                WHERE n->>'type' = 'start'
                LIMIT 1
            ) AS start_idx
        FROM agents a
        JOIN agent_kbs k ON k.agent_id = a.id
        WHERE a.workflow IS NOT NULL
          AND a.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements(a.workflow->'nodes') n
              WHERE jsonb_typeof(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) = 'array'
                AND jsonb_array_length(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) > 0
          )
    ),
    updated AS (
        UPDATE agents a
        SET
            workflow = jsonb_set(
                a.workflow,
                ARRAY['nodes', t.start_idx::text, 'data', 'knowledge_base_ids'],
                t.kb_ids,
                true
            ),
            workflow_draft = CASE
                WHEN a.workflow_draft IS NULL THEN NULL
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(a.workflow_draft->'nodes') n
                    WHERE jsonb_typeof(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) = 'array'
                      AND jsonb_array_length(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) > 0
                ) AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(a.workflow_draft->'nodes') n
                    WHERE n->>'type' = 'start'
                ) THEN jsonb_set(
                    a.workflow_draft,
                    ARRAY[
                        'nodes',
                        (
                            SELECT (ord - 1)::text
                            FROM jsonb_array_elements(a.workflow_draft->'nodes')
                                WITH ORDINALITY AS d(n, ord)
                            WHERE n->>'type' = 'start'
                            LIMIT 1
                        ),
                        'data',
                        'knowledge_base_ids'
                    ],
                    t.kb_ids,
                    true
                )
                ELSE a.workflow_draft
            END
        FROM targets t
        WHERE a.id = t.id AND t.start_idx IS NOT NULL
        RETURNING a.id
    )
    SELECT COUNT(*) INTO patched FROM updated;

    IF patched > 0 THEN
        RAISE NOTICE 'kb workflow backfill patched % agent(s)', patched;
    END IF;
END
$kb_workflow_backfill$;


-- A KB's embedding provider must belong to the KB's tenant.
DO $$
DECLARE violations int;
BEGIN
  SELECT count(*) INTO violations
    FROM knowledge_bases k JOIN provider_configs p ON p.id = k.embedding_config_id
   WHERE p.tenant_id IS DISTINCT FROM k.tenant_id;
  IF violations > 0 THEN
    RAISE EXCEPTION
      'knowledge_bases has % embedding_config_id(s) pointing at another tenant''s provider; '
      'fix them before applying knowledge_schema.sql', violations;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'knowledge_bases_embedding_config_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE knowledge_bases ADD CONSTRAINT knowledge_bases_embedding_config_tenant_fkey
      FOREIGN KEY (embedding_config_id, tenant_id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
END $$;
