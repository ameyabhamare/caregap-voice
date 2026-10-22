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
