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

CREATE INDEX IF NOT EXISTS outreach_scripts_review_idx
    ON outreach_scripts (created_at) WHERE status = 'needs_review';
