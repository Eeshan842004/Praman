"""Ledger record contract (schema audit).

The ledger is append-only, so an outcome CANNOT be an update to a decision row.
Three entry types share one hash chain, discriminated by `entry_type`:

    DECISION  -- what we inferred and what policy authorised
    ACTUATION -- what we actually did (compliance counters read ONLY these)
    OUTCOME   -- what happened, including natural recovery in the holdout

These tests are the audit. Anything the orchestrator will need on Day 6 has to
be a hashed column today, because ledger.db is throwaway now and expensive to
change later.

Written before the implementation exists.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from praman.ledger.canonical import canonical_bytes
from praman.ledger.chain import FIELDS, append, connect, verify
from praman.ledger.records import (
    LEDGER_SCHEMA_VERSION,
    ActuationRecord,
    DecisionRecord,
    EntryType,
    OutcomeRecord,
    prob_vector_json,
)
from praman.taxonomy import CAUSES

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
POSTERIOR = dict.fromkeys(CAUSES, 1 / 9)


def _decision(**kw) -> DecisionRecord:
    base = dict(
        ts_ms=1787000000000,
        experiment_id="praman-v1",
        holdout_pct=10,
        payment_id="pay_TEST000001",
        customer_id="cust_0007",
        arm="treatment",
        attempt_no=1,
        rail="card",
        symbol="05",
        region="IN",
        cause="INSUFFICIENT_FUNDS",
        posterior=POSTERIOR,
        attribution_source="heuristic",
        attribution_version="taxonomy-v1",
        tier="T1",
        tier_evaluations={"T1": [], "T2": ["no_alternate_instrument"]},
        opa_allow=True,
        deny_reasons=[],
        policy_input={"cause_class": "soft", "attempts_30d": 3},
        bundle_revision="4ca4787c0a1eea75",
        decision_id="8e1692ae-9cf9-43b5-b1d0-e4eeb3abc6ae",
        amount_paise=2200000,
        cuped_covariate=0.83,
        covariate_asof_ms=1786900000000,
        scheduled_for_ms=1787003600000,
        payload={"redacted": True},
    )
    base.update(kw)
    return DecisionRecord(**base)


def _actuation(**kw) -> ActuationRecord:
    base = dict(
        ts_ms=1787003600000,
        experiment_id="praman-v1",
        holdout_pct=10,
        payment_id="pay_TEST000001",
        customer_id="cust_0007",
        arm="treatment",
        decision_seq=1,
        attempt_no=1,
        rail="card",
        tier="T1",
        executed=True,
        actuation_result="failure",
    )
    base.update(kw)
    return ActuationRecord(**base)


def _outcome(**kw) -> OutcomeRecord:
    base = dict(
        ts_ms=1787090000000,
        experiment_id="praman-v1",
        holdout_pct=10,
        payment_id="pay_TEST000001",
        customer_id="cust_0007",
        arm="treatment",
        decision_seq=1,
        recovered=True,
        recovered_at_ms=1787090000000,
        recovered_amount_paise=2200000,
        outcome_source="actuated",
    )
    base.update(kw)
    return OutcomeRecord(**base)


ALL = (_decision, _actuation, _outcome)


# ─────────────────────────────────────────────────────────────────────────────
# Every record must fit the hashed column set exactly
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("factory", ALL)
def test_row_keys_match_hashed_fields_exactly(factory):
    """No field may exist on a record that the hash does not cover, and no
    hashed column may go unfilled. A column outside the hash is not evidence."""
    assert set(factory().to_row()) == set(FIELDS)


@pytest.mark.parametrize("factory", ALL)
def test_row_is_ledger_safe(factory):
    canonical_bytes(factory().to_row())


@pytest.mark.parametrize("factory", ALL)
def test_schema_version_is_stamped(factory):
    assert factory().to_row()["schema_version"] == LEDGER_SCHEMA_VERSION


def test_entry_types_are_distinct_and_correct():
    assert _decision().to_row()["entry_type"] == EntryType.DECISION
    assert _actuation().to_row()["entry_type"] == EntryType.ACTUATION
    assert _outcome().to_row()["entry_type"] == EntryType.OUTCOME


# ─────────────────────────────────────────────────────────────────────────────
# Fields the orchestrator will need on Day 6 (the actual audit)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "field",
    [
        "entry_type",
        "experiment_id",
        "holdout_pct",  # arm = f(experiment_id, customer_id, holdout_pct)
        "attempt_no",
        "rail",
        "scheduled_for_ms",
        "attribution_source",
        "cuped_covariate",
        "recovered",
        "recovered_at_ms",
        "recovered_amount_paise",
        # Caught by this audit, not on the original list:
        "symbol",  # the evidence the posterior was computed from
        "region",  # posterior depends on the regional prior
        "posterior_vector",  # storing only argmax destroys the ambiguity claim
        "tier_evaluations",  # the S3 deadlock demo needs ALL tier deny-sets
        "policy_input_json",  # replay CANNOT re-evaluate without the input
        "attribution_version",  # which attributor, same logic as bundle_revision
        "covariate_asof_ms",  # PROVES the covariate is pre-treatment
        "decision_seq",  # provenance in an append-only model
        "outcome_source",  # separates natural recovery from caused recovery
        "schema_version",  # future migrations must identify old rows
    ],
)
def test_required_field_is_present_and_hashed(field: str):
    assert field in FIELDS


def test_policy_input_is_stored_so_replay_can_re_evaluate():
    """`praman verify` replays stored inputs against the pinned bundle. Without
    the input persisted, replay attestation is only a hash check -- half the
    claim."""
    row = _decision().to_row()
    assert json.loads(row["policy_input_json"])["attempts_30d"] == 3


def test_posterior_vector_carries_all_nine_causes():
    row = _decision().to_row()
    assert set(json.loads(row["posterior_vector"])) == set(CAUSES)


def test_posterior_scalar_is_the_max_of_the_vector():
    rec = _decision(posterior={**POSTERIOR, "INSUFFICIENT_FUNDS": 0.56})
    row = rec.to_row()
    vec = json.loads(row["posterior_vector"])
    assert row["posterior"] == max(vec.values())


def test_tier_evaluations_record_every_tier_not_just_the_chosen_one():
    row = _decision(
        tier="T4",
        tier_evaluations={
            "T1": ["rbi_afa_required", "npci_autopay_blackout_window"],
            "T2": ["no_alternate_instrument"],
            "T3": ["nudge_fatigue_7d", "rbi_pre_debit_notice_not_elapsed"],
            "T4": [],
        },
    ).to_row()
    evals = json.loads(row["tier_evaluations"])
    assert set(evals) == {"T1", "T2", "T3", "T4"}
    assert len(evals["T1"]) == 2


def test_covariate_must_predate_the_decision():
    """CUPED is only unbiased if the covariate is strictly pre-treatment. The
    record refuses to assert it -- it timestamps it."""
    with pytest.raises(ValueError, match="pre-treatment"):
        _decision(covariate_asof_ms=1787000000001)  # after ts_ms


# ─────────────────────────────────────────────────────────────────────────────
# Law #5 -- no floats reach the ledger
# ─────────────────────────────────────────────────────────────────────────────
def test_probabilities_are_serialised_as_six_dp_strings():
    row = _decision().to_row()
    assert row["posterior"] == "0.111111"
    assert row["cuped_covariate"] == "0.830000"
    assert isinstance(json.loads(row["posterior_vector"])["INSUFFICIENT_FUNDS"], str)


def test_money_is_integer_paise():
    assert isinstance(_decision().to_row()["amount_paise"], int)
    assert isinstance(_outcome().to_row()["recovered_amount_paise"], int)


def test_booleans_are_stored_as_ints_not_bools():
    """SQLite has no bool. Storing a Python bool round-trips as int anyway, so
    normalising here keeps the hashed bytes identical on write and on replay."""
    row = _decision().to_row()
    assert row["opa_allow"] in (0, 1) and not isinstance(row["opa_allow"], bool)
    assert _outcome().to_row()["recovered"] in (0, 1)


def test_prob_vector_json_is_key_sorted_and_stable():
    a = prob_vector_json({"B": 0.5, "A": 0.5})
    b = prob_vector_json({"A": 0.5, "B": 0.5})
    assert a == b == '{"A":"0.500000","B":"0.500000"}'


# ─────────────────────────────────────────────────────────────────────────────
# GOLDEN FILE -- the canonical byte format is locked
# ─────────────────────────────────────────────────────────────────────────────
def test_canonical_serialisation_is_byte_locked():
    """If this hash changes, every previously written ledger becomes
    unverifiable. Changing it is a migration, never an edit."""
    digest = hashlib.sha256(canonical_bytes(_decision().to_row())).hexdigest()
    assert digest == "bc70f63b55a978920d3ec5057d90e4a2b363e4eca8ee6806a0339f70c52d7cc4", (
        f"canonical bytes changed -> {digest}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# End to end through the real chain
# ─────────────────────────────────────────────────────────────────────────────
def test_all_three_record_types_append_to_one_chain(tmp_path):
    conn = connect(tmp_path / "l.db")
    try:
        append(conn, _decision().to_row())
        append(conn, _actuation().to_row())
        append(conn, _outcome().to_row())

        ok, _, msg = verify(conn)
        assert ok, msg

        types = [r[0] for r in conn.execute("SELECT entry_type FROM ledger ORDER BY seq")]
        assert types == ["DECISION", "ACTUATION", "OUTCOME"]
    finally:
        conn.close()


def test_outcome_is_a_new_entry_never_an_update(tmp_path):
    """The append-only trigger already blocks UPDATE. This asserts the DESIGN
    consequence: recording an outcome grows the chain rather than mutating it."""
    conn = connect(tmp_path / "l.db")
    try:
        append(conn, _decision().to_row())
        height_before = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        append(conn, _outcome().to_row())
        height_after = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        assert height_after == height_before + 1
        assert verify(conn)[0]
    finally:
        conn.close()


def test_holdout_outcomes_are_recorded_as_natural(tmp_path):
    """Natural recovery in the holdout is the counterfactual baseline. If it is
    not recorded, there is nothing to subtract and the estimate is gross."""
    conn = connect(tmp_path / "l.db")
    try:
        append(conn, _outcome(arm="holdout", outcome_source="natural").to_row())
        row = conn.execute("SELECT arm, outcome_source FROM ledger").fetchone()
        assert row == ("holdout", "natural")
    finally:
        conn.close()
