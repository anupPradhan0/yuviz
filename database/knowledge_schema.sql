-- Voice AI Platform PostgreSQL schema — Phase 6A: Knowledge Platform
-- Run: psql voiceai -f database/knowledge_schema.sql (after database/schema.sql)
-- Idempotent: safe to run repeatedly against an existing database.
-- Additive only — does not modify any existing table's columns, only adds
-- 'embedding' to provider_configs.role's allowed values (a new provider
-- role, same table, same pattern already used for stt/llm/tts).

CREATE EXTENSION IF NOT EXISTS vector;

-- provider_configs.role gains 'embedding' as a fourth allowed role — an
-- embedding provider is configured and cached exactly like stt/llm/tts
-- (see services/knowledge/embedding_manager.py), just never referenced by
-- tenants.default_*_config_id or agents.*_config_id (no column for it
-- there — a KB's embedding provider is chosen on the knowledge_bases row
-- instead, since a tenant may run multiple KBs against different models).
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
    -- Same reversible-pause semantics as agents.status: an inactive KB's
    -- rows/history stay intact, it just stops being eligible for retrieval.
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
-- source_ref points at StorageProvider-managed content (see
-- libs/knowledge_sdk/models.py) — a local file path today, an S3/GCS key
-- once a cloud StorageProvider is added; raw bytes never live in Postgres.
CREATE TABLE IF NOT EXISTS kb_documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),  -- denormalized for cheap tenant-scoped queries/RLS-style checks
    title         TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    content_type  TEXT NOT NULL,  -- e.g. 'application/pdf', 'text/plain', 'text/markdown'
    language      TEXT,
    tags          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- pending: uploaded, not yet queued/chunked. processing: ingestion job
    -- running. ready: chunks+embeddings exist, eligible for retrieval.
    -- failed: ingestion_jobs row has the error; document stays visible so
    -- an admin can retry rather than silently vanishing.
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error         TEXT,
    version       INT NOT NULL DEFAULT 1,  -- bumped on re-upload/re-ingest; kb_chunks.version tags which pass produced a chunk
    -- auto: eligible for vector retrieval only, surfaced when relevant to
    -- the query (the only mode that existed before this column). prompt:
    -- always injected into the LLM context every turn, regardless of
    -- query relevance — set explicitly by an admin (small/critical
    -- reference docs), or automatically by the ingestion worker for
    -- documents under AUTO_INLINE_THRESHOLD_BYTES (see ingestion_worker.py)
    -- where embedding + vector search would add latency for no benefit.
    usage_mode    TEXT NOT NULL DEFAULT 'auto' CHECK (usage_mode IN ('auto', 'prompt')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

-- CREATE TABLE IF NOT EXISTS above doesn't add a column to a kb_documents
-- table that already existed before this file gained usage_mode — keeps
-- the file safe to re-run against a DB from before that point, matching
-- database/schema.sql's own re-run-safety convention (e.g. agents.status).
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS usage_mode TEXT NOT NULL DEFAULT 'auto';
DO $$ BEGIN
    ALTER TABLE kb_documents ADD CONSTRAINT kb_documents_usage_mode_check CHECK (usage_mode IN ('auto', 'prompt'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_kb_documents_kb ON kb_documents(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant ON kb_documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_status ON kb_documents(status);

-- ── kb_chunks — one row per chunk, with its embedding ────────────────────────
-- 768 dims matches nomic-embed-text (via Ollama) — the one embedding
-- provider actually configured on this machine today (local, no API key
-- required, consistent with faster_whisper/Ollama-LLM/macOS-TTS already
-- being the local-first default engines elsewhere in this project). A KB
-- using a different-dimension model (e.g. OpenAI text-embedding-3-small,
-- 1536) is a forward-compatible gap (would need a second vector column or
-- a per-KB table), not solved here: no such provider is configured yet, so
-- there is nothing real to generalize for.
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
-- HNSW over cosine distance — matches the retrieval query's <=> operator
-- (see services/knowledge/retrieval.py). Built once here rather than left
-- to a maintenance script: the table starts empty in every environment
-- this runs against, so there's no "reindex a huge table" cost to defer.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- ── agent_knowledge_bases — which KBs an agent retrieves from ───────────────
-- A join table, not a column on agents: an agent may use zero, one, or
-- several KBs. enabled lets a KB be attached but temporarily paused for one
-- agent without detaching it (mirrors agents.status's reversible-pause
-- pattern at the assignment level).
CREATE TABLE IF NOT EXISTS agent_knowledge_bases (
    agent_id    UUID NOT NULL REFERENCES agents(id),
    kb_id       UUID NOT NULL REFERENCES knowledge_bases(id),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, kb_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_kb_agent ON agent_knowledge_bases(agent_id) WHERE enabled;

-- ── agent_retrieval_policies — per-agent RetrievalPolicy, never hardcoded ──
-- One row per agent that has customized retrieval behavior (a Support
-- agent wanting top_k=8, a Legal agent wanting top_k=15 and stricter
-- minimum_score, a Sales agent wanting top_k=3 for speed) — see
-- services/knowledge/retrieval.py's _resolve_policy() for the full
-- override chain (per-call override > this row > system default). No row
-- for an agent means "use the system default", not "retrieval is broken" —
-- same "absence is a normal, cheap default" posture as every other
-- optional config surface in this project.
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
-- Same polling-loop precedent as services/config/phone_numbers.py's
-- prewarm() — no Celery/RQ/broker introduced for one background worker.
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
