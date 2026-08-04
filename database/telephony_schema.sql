-- Voice AI Platform PostgreSQL schema — pluggable telephony provider configuration
-- Run: psql voiceai -f database/telephony_schema.sql (after database/schema.sql)
-- Idempotent: safe to run repeatedly against an existing database.
-- Additive only — one new table, plus one nullable column added to the
-- existing phone_numbers table.

-- ── telephony_configs ────────────────────────────────────────────────────────
-- One row per telephony-provider account a tenant has configured (e.g. one
-- Vobiz account, or later a Twilio/Telnyx account). `credentials` is
-- free-form JSONB, validated per-provider by libs/telephony_sdk's registry
-- at the application layer — matches provider_configs.extra's existing
-- convention exactly, rather than a DB-level discriminated union.
CREATE TABLE IF NOT EXISTS telephony_configs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    provider            TEXT NOT NULL,
    credentials         JSONB NOT NULL DEFAULT '{}',
    is_default_outbound BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

-- At most one default-outbound config per tenant, enforced by the database
-- itself (not just application logic) — mirrors Dograh's own partial unique
-- index for exactly this same field.
CREATE UNIQUE INDEX IF NOT EXISTS idx_telephony_configs_default_outbound
    ON telephony_configs(tenant_id) WHERE is_default_outbound AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_telephony_configs_tenant ON telephony_configs(tenant_id);

-- Which telephony-provider account a given DID belongs to. Nullable: a DID
-- routed purely through the Gateway/Kamailio/FreeSWITCH path (no REST/
-- webhook provider involved) has no telephony_config at all. Extends the
-- existing phone_numbers table rather than introducing a second numbers
-- table (phone_numbers already owns DID->agent routing and its Redis
-- write-through cache-aside pattern) — extend, don't duplicate.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS telephony_config_id UUID REFERENCES telephony_configs(id);
