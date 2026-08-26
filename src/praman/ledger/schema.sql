-- Praman ledger. This file is not a database schema, it is an evidence format.
--
-- Two constraints carry the whole audit claim:
--   UNIQUE(prev_hash)  -- a fork becomes an IntegrityError at INSERT time rather
--                         than silent corruption discovered a week later (S1).
--   append-only triggers -- immutability enforced by storage, not by convention.
--
-- Every column that could change a decision is covered by entry_hash. Anything
-- not covered is not evidence.

CREATE TABLE IF NOT EXISTS ledger (
    seq             INTEGER PRIMARY KEY,       -- gapless, monotonic
    ts_ms           INTEGER NOT NULL,          -- epoch ms, never a float
    payment_id      TEXT    NOT NULL,
    customer_id     TEXT    NOT NULL,          -- randomisation unit (law #8)
    arm             TEXT    NOT NULL,          -- 'treatment' | 'holdout'
    cause           TEXT    NOT NULL,
    posterior       TEXT    NOT NULL,          -- 6-dp string, never a float
    tier            TEXT    NOT NULL,
    opa_allow       INTEGER NOT NULL,          -- 0/1
    deny_reasons    TEXT    NOT NULL,          -- sorted JSON array
    bundle_revision TEXT    NOT NULL,          -- as REPORTED BY OPA (law #6)
    decision_id     TEXT    NOT NULL,
    amount_paise    INTEGER NOT NULL,          -- integer paise, never a float
    payload_json    TEXT    NOT NULL,          -- canonical bytes, redacted
    prev_hash       TEXT    NOT NULL UNIQUE,   -- makes a fork a constraint violation
    entry_hash      TEXT    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS ledger_payment_idx  ON ledger(payment_id);
CREATE INDEX IF NOT EXISTS ledger_customer_idx ON ledger(customer_id);
CREATE INDEX IF NOT EXISTS ledger_revision_idx ON ledger(bundle_revision);

-- Append-only, enforced at storage level.
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
