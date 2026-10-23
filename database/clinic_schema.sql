-- Clinic appointments schema: departments, doctors, OPD schedules, leave, appointments,
-- and the outbox that syncs them to Google Calendar, Twilio SMS and reminder calls.
-- Run: psql voiceai -f database/clinic_schema.sql (after schema.sql, before rls.sql). Idempotent.
--
-- These tables are the source of truth for availability. Google Calendar is a copy
-- written by the clinic service's outbox worker, never read back.

-- Needs superuser once; lets the exclusion constraint below compare doctor_id with '='.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ── clinic_settings — one row per tenant that runs a clinic ──────────────────
CREATE TABLE IF NOT EXISTS clinic_settings (
    tenant_id               UUID PRIMARY KEY REFERENCES tenants(id),
    hospital_name           TEXT NOT NULL,
    hospital_name_hi        TEXT,
    -- Front-desk number quoted in SMS ("call us to reschedule").
    hospital_phone          TEXT,
    timezone                TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    booking_window_days     INT  NOT NULL DEFAULT 15 CHECK (booking_window_days BETWEEN 1 AND 60),
    min_lead_minutes        INT  NOT NULL DEFAULT 60 CHECK (min_lead_minutes >= 0),
    -- A slot offered in a read-back is held for this caller so nobody takes it mid-confirmation.
    hold_seconds            INT  NOT NULL DEFAULT 180 CHECK (hold_seconds BETWEEN 30 AND 900),
    max_active_per_phone    INT  NOT NULL DEFAULT 4 CHECK (max_active_per_phone >= 1),
    -- Twilio SMS. The auth token is a secret ref ('env:' | 'enc:' | 'k8s:'), never the token.
    sms_enabled             BOOLEAN NOT NULL DEFAULT false,
    twilio_account_sid      TEXT,
    twilio_auth_token_ref   TEXT,
    twilio_from             TEXT,
    -- Google Calendar sync, through toolexec's connection; NULL = sync off.
    google_connection_id    UUID,
    -- Reminder calls: dial the patient, then bridge to reminder_agent_did.
    reminder_enabled        BOOLEAN NOT NULL DEFAULT false,
    reminder_hours_before   INT  NOT NULL DEFAULT 24 CHECK (reminder_hours_before BETWEEN 1 AND 72),
    reminder_agent_did      TEXT,
    reminder_caller_id      TEXT,
    reminder_window_start   TIME NOT NULL DEFAULT '09:00',
    reminder_window_end     TIME NOT NULL DEFAULT '20:00',
    reminder_max_attempts   INT  NOT NULL DEFAULT 3 CHECK (reminder_max_attempts BETWEEN 1 AND 5),
    reminder_retry_minutes  INT  NOT NULL DEFAULT 30 CHECK (reminder_retry_minutes >= 5),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── clinic_api_keys — how toolexec's calls prove which tenant they are for ───
-- Only the SHA-256 of the key is stored; the key itself lives in custom_apis.auth_config as an enc: ref.
CREATE TABLE IF NOT EXISTS clinic_api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    key_hash    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

-- ── clinic_departments ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinic_departments (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    code         TEXT NOT NULL,
    name         TEXT NOT NULL,
    name_hi      TEXT,
    description  TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    -- Plain-language complaints this department sees first; feeds the agent's suggestions.
    symptoms     TEXT[] NOT NULL DEFAULT '{}',
    sort_order   INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_departments_code
    ON clinic_departments(tenant_id, lower(code)) WHERE deleted_at IS NULL;

-- ── clinic_doctors ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinic_doctors (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    department_id       UUID NOT NULL REFERENCES clinic_departments(id),
    code                TEXT NOT NULL,
    name                TEXT NOT NULL,
    name_hi             TEXT,
    gender              TEXT CHECK (gender IN ('female', 'male', 'other')),
    qualifications      TEXT NOT NULL DEFAULT '',
    specialization      TEXT NOT NULL DEFAULT '',
    experience_years    INT CHECK (experience_years >= 0),
    languages           TEXT[] NOT NULL DEFAULT '{en,hi}',
    consultation_fee    INT CHECK (consultation_fee >= 0),
    slot_minutes        INT NOT NULL DEFAULT 15 CHECK (slot_minutes BETWEEN 5 AND 120),
    bio                 TEXT NOT NULL DEFAULT '',
    google_calendar_id  TEXT,
    active              BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_doctors_code
    ON clinic_doctors(tenant_id, lower(code)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_clinic_doctors_department
    ON clinic_doctors(department_id) WHERE deleted_at IS NULL;

-- ── clinic_doctor_schedules — weekly OPD hours (weekday 0 = Monday) ─────────
CREATE TABLE IF NOT EXISTS clinic_doctor_schedules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    doctor_id   UUID NOT NULL REFERENCES clinic_doctors(id),
    weekday     SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    room        TEXT NOT NULL DEFAULT '',
    valid_from  DATE,
    valid_to    DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (start_time < end_time),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to)
);

CREATE INDEX IF NOT EXISTS idx_clinic_doctor_schedules_doctor ON clinic_doctor_schedules(doctor_id, weekday);

-- ── clinic_doctor_leaves — leave, conferences, blocked hours ────────────────
CREATE TABLE IF NOT EXISTS clinic_doctor_leaves (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    doctor_id   UUID NOT NULL REFERENCES clinic_doctors(id),
    starts_at   TIMESTAMPTZ NOT NULL,
    ends_at     TIMESTAMPTZ NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at)
);

CREATE INDEX IF NOT EXISTS idx_clinic_doctor_leaves_doctor ON clinic_doctor_leaves(doctor_id, ends_at);

-- ── clinic_appointments ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinic_appointments (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    reference          TEXT NOT NULL,
    doctor_id          UUID NOT NULL REFERENCES clinic_doctors(id),
    patient_name       TEXT NOT NULL,
    patient_phone      TEXT NOT NULL,
    -- The number the booking came from (caller ID); may differ when booking for family.
    booked_by_phone    TEXT,
    patient_age        INT CHECK (patient_age BETWEEN 0 AND 130),
    reason             TEXT NOT NULL DEFAULT '',
    language           TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'hi')),
    channel            TEXT NOT NULL DEFAULT 'phone' CHECK (channel IN ('phone', 'admin', 'reminder')),
    starts_at          TIMESTAMPTZ NOT NULL,
    ends_at            TIMESTAMPTZ NOT NULL,
    status             TEXT NOT NULL DEFAULT 'booked'
                       CHECK (status IN ('booked', 'cancelled', 'completed', 'no_show')),
    cancel_reason      TEXT,
    -- Reminder call state; next_reminder_at NULL = nothing scheduled.
    reminder_status    TEXT NOT NULL DEFAULT 'pending'
                       CHECK (reminder_status IN ('pending', 'calling', 'confirmed', 'declined',
                                                  'rescheduled', 'cancelled', 'unreachable', 'skipped')),
    reminder_attempts  INT NOT NULL DEFAULT 0,
    next_reminder_at   TIMESTAMPTZ,
    gcal_event_id      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at       TIMESTAMPTZ,
    CHECK (starts_at < ends_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_appointments_reference
    ON clinic_appointments(tenant_id, reference);
CREATE INDEX IF NOT EXISTS idx_clinic_appointments_doctor_time
    ON clinic_appointments(doctor_id, starts_at) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS idx_clinic_appointments_patient_phone
    ON clinic_appointments(tenant_id, patient_phone, starts_at);
CREATE INDEX IF NOT EXISTS idx_clinic_appointments_booked_by
    ON clinic_appointments(tenant_id, booked_by_phone, starts_at);
CREATE INDEX IF NOT EXISTS idx_clinic_appointments_reminder_due
    ON clinic_appointments(next_reminder_at) WHERE status = 'booked' AND next_reminder_at IS NOT NULL;

-- The double-booking guard: two live bookings for one doctor can never overlap, whatever
-- the application does. A concurrent second INSERT fails with exclusion_violation (23P01).
DO $$
BEGIN
    ALTER TABLE clinic_appointments ADD CONSTRAINT clinic_appointments_no_overlap
        EXCLUDE USING gist (doctor_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
        WHERE (status = 'booked');
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
END $$;

-- ── clinic_slot_holds — a slot read back to a caller, held while they say yes ─
CREATE TABLE IF NOT EXISTS clinic_slot_holds (
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    doctor_id     UUID NOT NULL REFERENCES clinic_doctors(id),
    starts_at     TIMESTAMPTZ NOT NULL,
    holder_phone  TEXT NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (doctor_id, starts_at)
);

-- ── clinic_outbox — side effects that must survive a Google or Twilio outage ─
CREATE TABLE IF NOT EXISTS clinic_outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    appointment_id   UUID REFERENCES clinic_appointments(id),
    kind             TEXT NOT NULL CHECK (kind IN ('gcal_sync', 'sms')),
    payload          JSONB NOT NULL DEFAULT '{}',
    dedupe_key       TEXT NOT NULL,
    -- Bumped each time a change re-arms the row; the worker marks it done only if rev is unchanged.
    rev              INT NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
    attempts         INT NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_outbox_dedupe ON clinic_outbox(tenant_id, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_clinic_outbox_due ON clinic_outbox(next_attempt_at) WHERE status = 'pending';

-- ── clinic_reminder_calls — one row per dial attempt ────────────────────────
CREATE TABLE IF NOT EXISTS clinic_reminder_calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    appointment_id  UUID NOT NULL REFERENCES clinic_appointments(id),
    attempt         INT NOT NULL,
    job_uuid        TEXT,
    status          TEXT NOT NULL DEFAULT 'dialing'
                    CHECK (status IN ('dialing', 'answered', 'failed', 'no_answer')),
    detail          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_clinic_reminder_calls_job ON clinic_reminder_calls(job_uuid);

-- ── Same-tenant guards — the FKs are tenant-blind ───────────────────────────
-- Runs as the invoking role: a parent row the caller cannot see under RLS is refused too.
CREATE OR REPLACE FUNCTION clinic_same_tenant_doctor() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM clinic_doctors d WHERE d.id = NEW.doctor_id AND d.tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'doctor % does not belong to tenant %', NEW.doctor_id, NEW.tenant_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION clinic_same_tenant_department() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM clinic_departments d
                    WHERE d.id = NEW.department_id AND d.tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'department % does not belong to tenant %', NEW.department_id, NEW.tenant_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION clinic_same_tenant_appointment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.appointment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_appointments a WHERE a.id = NEW.appointment_id AND a.tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'appointment % does not belong to tenant %', NEW.appointment_id, NEW.tenant_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS clinic_doctors_same_tenant ON clinic_doctors;
CREATE TRIGGER clinic_doctors_same_tenant
    BEFORE INSERT OR UPDATE OF department_id, tenant_id ON clinic_doctors
    FOR EACH ROW EXECUTE FUNCTION clinic_same_tenant_department();

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['clinic_doctor_schedules', 'clinic_doctor_leaves', 'clinic_appointments',
                             'clinic_slot_holds'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_same_tenant', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF doctor_id, tenant_id ON %I '
            'FOR EACH ROW EXECUTE FUNCTION clinic_same_tenant_doctor()', t || '_same_tenant', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['clinic_outbox', 'clinic_reminder_calls'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_same_tenant', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF appointment_id, tenant_id ON %I '
            'FOR EACH ROW EXECUTE FUNCTION clinic_same_tenant_appointment()', t || '_same_tenant', t);
    END LOOP;
END $$;
