-- Praman ledger. This file is not a database schema, it is an evidence format.
--
-- Three entry types share ONE hash chain, discriminated by entry_type:
--   DECISION   what we inferred and what policy authorised (written BEFORE acting)
--   ACTUATION  what we actually did  <- compliance counters read ONLY these
--   OUTCOME    what happened, including natural recovery in the holdout
--
-- The ledger is append-only, so an outcome cannot be an UPDATE to a decision.
-- Columns not applicable to an entry type are NULL, never absent, so all three
-- types serialise over an identical key set and the hash stays uniform.
--
-- Two constraints carry the audit claim:
--   UNIQUE(prev_hash)     a fork becomes an IntegrityError at INSERT time (S1)
--   append-only triggers  immutability enforced by storage, not by convention

CREATE TABLE IF NOT EXISTS ledger (
    seq                    INTEGER PRIMARY KEY,   -- gapless, monotonic
    schema_version         INTEGER NOT NULL,      -- migrations must identify old rows
    entry_type             TEXT    NOT NULL,      -- DECISION | ACTUATION | OUTCOME

    -- Envelope (every entry) -------------------------------------------------
    ts_ms                  INTEGER NOT NULL,      -- epoch ms, never a float
    experiment_id          TEXT    NOT NULL,      -- arm = f(exp, customer, pct)
    holdout_pct            INTEGER NOT NULL,      -- ...so all three are recorded
    payment_id             TEXT    NOT NULL,
    customer_id            TEXT    NOT NULL,      -- randomisation unit (law #8)
    arm                    TEXT    NOT NULL,      -- treatment | holdout

    -- DECISION ---------------------------------------------------------------
    attempt_no             INTEGER,
    rail                   TEXT,                  -- card | upi | upi_autopay
    symbol                 TEXT,                  -- observed code the posterior came from
    region                 TEXT,                  -- posterior depends on regional prior
    cause                  TEXT,                  -- argmax of the posterior
    posterior              TEXT,                  -- 6-dp string, never a float
    posterior_vector       TEXT,                  -- all 9 causes; argmax alone hides the ambiguity
    attribution_source     TEXT,                  -- heuristic | ml
    attribution_version    TEXT,                  -- which attributor produced it
    tier                   TEXT,                  -- T0..T4 selected
    tier_evaluations       TEXT,                  -- EVERY tier's deny-set, not just the chosen one
    opa_allow              INTEGER,               -- 0/1
    deny_reasons           TEXT,                  -- sorted JSON array
    policy_input_json      TEXT,                  -- replay re-evaluates THIS against the bundle
    bundle_revision        TEXT,                  -- as REPORTED BY OPA (law #6)
    decision_id            TEXT,                  -- OPA's own decision id
    amount_paise           INTEGER,               -- integer paise, never a float
    cuped_covariate        TEXT,                  -- 6-dp string
    covariate_asof_ms      INTEGER,               -- PROVES the covariate is pre-treatment
    scheduled_for_ms       INTEGER,               -- logical clock target

    -- ACTUATION --------------------------------------------------------------
    decision_seq           INTEGER,               -- provenance link (append-only has no FK-by-update)
    executed               INTEGER,               -- 0/1; law #7 counts only executed=1
    actuation_result       TEXT,                  -- success | failure | skipped

    -- OUTCOME ----------------------------------------------------------------
    recovered              INTEGER,               -- 0/1
    recovered_at_ms        INTEGER,
    recovered_amount_paise INTEGER,
    outcome_source         TEXT,                  -- actuated | natural | none

    payload_json           TEXT,                  -- canonical bytes, redacted

    -- Chain ------------------------------------------------------------------
    prev_hash              TEXT    NOT NULL UNIQUE,
    entry_hash             TEXT    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS ledger_payment_idx  ON ledger(payment_id);
CREATE INDEX IF NOT EXISTS ledger_customer_idx ON ledger(customer_id);
CREATE INDEX IF NOT EXISTS ledger_revision_idx ON ledger(bundle_revision);
CREATE INDEX IF NOT EXISTS ledger_type_idx     ON ledger(entry_type);
CREATE INDEX IF NOT EXISTS ledger_decision_idx ON ledger(decision_seq);

-- Append-only, enforced at storage level.
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
