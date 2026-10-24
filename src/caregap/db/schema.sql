-- Application schema. Idempotent: safe to run on every start.
-- Applied by docker/postgres/init on first container start and by caregap.db.apply_schema().

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS cost_ledger (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at     TIMESTAMPTZ,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
    -- Worst-case cost held against the budget while the call is in flight.
    estimated_usd  NUMERIC(12, 6) NOT NULL CHECK (estimated_usd >= 0),
    actual_usd     NUMERIC(12, 6) CHECK (actual_usd >= 0),
    input_tokens   INTEGER CHECK (input_tokens >= 0),
    output_tokens  INTEGER CHECK (output_tokens >= 0)
);

CREATE INDEX IF NOT EXISTS cost_ledger_created_at_idx ON cost_ledger (created_at);

-- Member registry, synced from the gold layer (synthetic data only).
CREATE TABLE IF NOT EXISTS members (
    member_id      TEXT PRIMARY KEY,
    given_name     TEXT,
    family_name    TEXT,
    birth_date     DATE NOT NULL,
    gender         TEXT,
    language       TEXT NOT NULL DEFAULT 'en',
    phone          TEXT,
    deceased_date  DATE,
    is_edge_case   BOOLEAN NOT NULL DEFAULT false,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Care-gap status per member, measure and measurement year, synced from gold.
-- A sync replaces every row of a measurement year in one transaction.
CREATE TABLE IF NOT EXISTS care_gaps (
    member_id         TEXT NOT NULL REFERENCES members (member_id),
    measure           TEXT NOT NULL CHECK (measure IN ('awv', 'eye_exam', 'statin')),
    measurement_year  INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('met', 'gap', 'not_eligible', 'excluded')),
    evidence_date     DATE,
    pdc               NUMERIC(6, 4) CHECK (pdc BETWEEN 0 AND 1),
    reason            TEXT NOT NULL,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, measure, measurement_year)
);

CREATE INDEX IF NOT EXISTS care_gaps_open_idx
    ON care_gaps (measurement_year, measure) WHERE status = 'gap';

-- Generated outreach scripts, with every attempt and its compliance result.
-- status: compliant | needs_review (generation) -> approved | rejected (human review).
-- pending_approval: compliant, but held for a human because it is among the first N
-- messages of a campaign (Phase 5).
CREATE TABLE IF NOT EXISTS outreach_scripts (
    id                BIGSERIAL PRIMARY KEY,
    member_id         TEXT NOT NULL REFERENCES members (member_id),
    measurement_year  INTEGER NOT NULL,
    measures          TEXT[] NOT NULL,
    status            TEXT NOT NULL
                      CHECK (status IN ('compliant', 'needs_review', 'approved', 'rejected')),
    script            TEXT NOT NULL,
    violations        JSONB NOT NULL DEFAULT '[]',
    attempts          JSONB NOT NULL,
    prompt_versions   JSONB NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL,
    output_tokens     INTEGER NOT NULL,
    latency_ms        INTEGER NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at       TIMESTAMPTZ,
    reviewer          TEXT,
    review_note       TEXT
);

-- Recreated on every apply so older databases get the current predicate (small table).
DROP INDEX IF EXISTS outreach_scripts_review_idx;
CREATE INDEX outreach_scripts_review_idx
    ON outreach_scripts (created_at) WHERE status IN ('needs_review', 'pending_approval');

-- Rendered audio for outreach scripts (files live under data/audio until Phase 11).
CREATE TABLE IF NOT EXISTS script_audio (
    id                      BIGSERIAL PRIMARY KEY,
    script_id               BIGINT NOT NULL REFERENCES outreach_scripts (id),
    provider                TEXT NOT NULL,
    voice                   TEXT NOT NULL,
    pronunciation_overrides BOOLEAN NOT NULL,
    path                    TEXT NOT NULL,
    duration_ms             INTEGER NOT NULL,
    latency_ms              INTEGER NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS script_audio_script_idx ON script_audio (script_id, created_at);

-- Phase 5: campaigns. Earlier databases have the narrower status check; replace it.
ALTER TABLE outreach_scripts DROP CONSTRAINT IF EXISTS outreach_scripts_status_check;
ALTER TABLE outreach_scripts ADD CONSTRAINT outreach_scripts_status_check CHECK (
    status IN ('compliant', 'needs_review', 'pending_approval', 'approved', 'rejected'));

-- Contact consent per member and channel. No row means unknown, and unknown means no call.
CREATE TABLE IF NOT EXISTS consent (
    member_id   TEXT NOT NULL REFERENCES members (member_id),
    channel     TEXT NOT NULL CHECK (channel IN ('voice')),
    status      TEXT NOT NULL CHECK (status IN ('granted', 'revoked')),
    -- synthetic-seed | dtmf | ...
    source      TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, channel)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL,
    measurement_year     INTEGER NOT NULL,
    -- Copied from the outreach profile at creation, so a running campaign is unaffected
    -- by later profile changes.
    approval_first_n     INTEGER NOT NULL,
    max_call_attempts    INTEGER NOT NULL,
    retry_delay_minutes  INTEGER NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per member per campaign: the member's place in the outreach state machine.
CREATE TABLE IF NOT EXISTS campaign_members (
    campaign_id     BIGINT NOT NULL REFERENCES campaigns (id),
    member_id       TEXT NOT NULL REFERENCES members (member_id),
    state           TEXT NOT NULL CHECK (state IN (
                        'selected', 'preparing', 'awaiting_review', 'ready', 'calling',
                        'retry_wait', 'completed', 'opted_out', 'escalated', 'failed',
                        'skipped')),
    -- Why a member was skipped or failed.
    reason          TEXT,
    script_id       BIGINT REFERENCES outreach_scripts (id),
    -- The script went through a human (first-N hold or failed compliance).
    human_gate      BOOLEAN NOT NULL DEFAULT false,
    calls_made      INTEGER NOT NULL DEFAULT 0,
    -- When the next call may be placed (ready / retry_wait).
    next_call_at    TIMESTAMPTZ,
    -- When the current state was entered; used to recover work stuck in preparing/calling.
    state_since     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, member_id)
);

CREATE INDEX IF NOT EXISTS campaign_members_work_idx
    ON campaign_members (state, next_call_at)
    WHERE state IN ('selected', 'preparing', 'awaiting_review', 'ready', 'calling',
                    'retry_wait');

-- Each outbound call. status follows Twilio's CallStatus; 'queued' until dispatched.
CREATE TABLE IF NOT EXISTS call_attempts (
    id            BIGSERIAL PRIMARY KEY,
    campaign_id   BIGINT NOT NULL,
    member_id     TEXT NOT NULL,
    attempt_no    INTEGER NOT NULL,
    script_id     BIGINT NOT NULL REFERENCES outreach_scripts (id),
    audio_id      BIGINT NOT NULL REFERENCES script_audio (id),
    call_sid      TEXT UNIQUE,
    status        TEXT NOT NULL CHECK (status IN (
                      'queued', 'dispatched', 'completed', 'no-answer', 'busy', 'failed',
                      'canceled')),
    digits        TEXT,
    duration_s    INTEGER,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (campaign_id, member_id) REFERENCES campaign_members (campaign_id, member_id),
    UNIQUE (campaign_id, member_id, attempt_no)
);

-- Interaction log: every telephony callback as received.
CREATE TABLE IF NOT EXISTS telephony_events (
    id               BIGSERIAL PRIMARY KEY,
    call_attempt_id  BIGINT REFERENCES call_attempts (id),
    kind             TEXT NOT NULL CHECK (kind IN ('status', 'gather')),
    payload          JSONB NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Work for people: members asking for a human, calls that failed for good, workflow errors.
-- Also an outbox: n8n delivers rows with notified_at IS NULL to the notification webhook.
CREATE TABLE IF NOT EXISTS escalations (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN (
                         'member_request', 'delivery_failed', 'workflow_error')),
    campaign_id      BIGINT REFERENCES campaigns (id),
    member_id        TEXT REFERENCES members (member_id),
    call_attempt_id  BIGINT REFERENCES call_attempts (id),
    detail           TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at      TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS escalations_outbox_idx ON escalations (id)
    WHERE notified_at IS NULL;

CREATE INDEX IF NOT EXISTS escalations_open_idx ON escalations (created_at)
    WHERE resolved_at IS NULL;

-- Phase 6: customers (customers/*.yaml). Rows from before Phase 6 belong to Sunrise,
-- the only customer then; new rows always name their customer.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS customer TEXT NOT NULL DEFAULT 'sunrise';
ALTER TABLE outreach_scripts ADD COLUMN IF NOT EXISTS customer TEXT NOT NULL DEFAULT 'sunrise';
ALTER TABLE outreach_scripts ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';

-- Phase 7: callback Q&A over each customer's Summary of Benefits.
-- Chunks are replaced per customer on every ingest. 768 = nomic-embed-text dimensions.
CREATE TABLE IF NOT EXISTS benefit_chunks (
    id           TEXT PRIMARY KEY,           -- <customer>:<section>-<n>, e.g. sunrise:S6-1
    customer     TEXT NOT NULL,
    section      TEXT NOT NULL,              -- S1..Sn
    title        TEXT NOT NULL,
    text         TEXT NOT NULL,
    embedding    vector(768) NOT NULL,
    source       TEXT NOT NULL,              -- document path
    source_sha256 TEXT NOT NULL,
    embed_model  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS benefit_chunks_customer_idx ON benefit_chunks (customer);

-- Interaction log for callback questions. answer is what the caller was told; draft is
-- the model's answer before the grounding check (NULL when it never got that far).
CREATE TABLE IF NOT EXISTS callback_questions (
    id               BIGSERIAL PRIMARY KEY,
    customer         TEXT NOT NULL,
    member_id        TEXT REFERENCES members (member_id),
    question         TEXT NOT NULL,
    language         TEXT NOT NULL,
    route            TEXT NOT NULL,
    outcome          TEXT NOT NULL,          -- answered | handoff
    reason           TEXT,                   -- why it was handed off
    answer           TEXT NOT NULL,
    draft            TEXT,
    citations        JSONB NOT NULL DEFAULT '[]',
    retrieved        JSONB NOT NULL DEFAULT '[]',
    prompt_versions  JSONB NOT NULL DEFAULT '{}',
    model            TEXT NOT NULL,
    latency_ms       INTEGER NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The one definition of the escalation kinds (re-applied on every start, so it must list
-- every kind ever written; agent_request is Phase 9).
ALTER TABLE escalations DROP CONSTRAINT IF EXISTS escalations_kind_check;
ALTER TABLE escalations ADD CONSTRAINT escalations_kind_check CHECK (kind IN (
    'member_request', 'delivery_failed', 'workflow_error', 'callback_question',
    'agent_request'));
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS callback_question_id BIGINT
    REFERENCES callback_questions (id);

-- Phase 9: the MCP server (caregap.mcp). An agent can log what happened in a contact with
-- a member and ask for a human. The agent's own words go in note/summary, never in
-- escalations.detail: notifications carry IDs only.
CREATE TABLE IF NOT EXISTS member_interactions (
    id          BIGSERIAL PRIMARY KEY,
    member_id   TEXT NOT NULL REFERENCES members (member_id),
    customer    TEXT NOT NULL,
    channel     TEXT NOT NULL CHECK (channel IN ('phone', 'sms', 'email', 'chat', 'other')),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'reached', 'left_message', 'no_answer', 'resolved', 'follow_up', 'other')),
    summary     TEXT NOT NULL,
    source      TEXT NOT NULL,              -- who logged it, e.g. mcp
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS member_interactions_member_idx
    ON member_interactions (member_id, created_at);

ALTER TABLE escalations ADD COLUMN IF NOT EXISTS note TEXT;
-- One open agent request per member and reason: repeated tool calls return the same row.
CREATE UNIQUE INDEX IF NOT EXISTS escalations_agent_request_once
    ON escalations (member_id, md5(note)) WHERE kind = 'agent_request' AND resolved_at IS NULL;
