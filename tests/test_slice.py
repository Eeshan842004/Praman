"""The vertical slice, end to end.

synthetic decline -> normalise -> posterior -> cluster arm -> ladder -> OPA ->
ledger DECISION -> simulated actuation -> ledger OUTCOME -> estimate_ate over
the real ledger.

No LightGBM anywhere: attribution is the taxonomy posterior. The model is an
upgrade to this path, not a dependency of it.

The estimand is INTENTION TO TREAT. A treatment-arm payment that policy refused
to act on still counts as treated, because the thing being measured is the
system as deployed -- refusals included. Reporting per-protocol instead would
quietly credit the agent for exactly the payments it declined to touch.

Written before the implementation exists.
"""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import rego_like_client

from praman.ledger.chain import connect, verify
from praman.measure.from_ledger import estimate_from_ledger, load_experiment
from praman.sim.generator import generate_batch
from praman.slice_runner import run_batch


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    path = tmp_path_factory.mktemp("slice") / "ledger.db"
    result = run_batch(
        n=2000, seed=42, ledger_path=path, client=rego_like_client(), experiment_id="slice-test"
    )
    return result, path


# ─────────────────────────────────────────────────────────────────────────────
# The generator seals what the pipeline must not see
# ─────────────────────────────────────────────────────────────────────────────
def test_generator_is_deterministic():
    a = generate_batch(n=50, seed=7)
    b = generate_batch(n=50, seed=7)
    assert [d.payment_id for d in a.declines] == [d.payment_id for d in b.declines]
    assert [d.latent_cause for d in a.declines] == [d.latent_cause for d in b.declines]


def test_generator_emits_both_potential_outcomes():
    for d in generate_batch(n=50, seed=7).declines:
        assert isinstance(d.y0_recovered, bool)
        assert isinstance(d.y1_recovered, bool)


def test_customers_repeat_so_clustering_is_real():
    """If every payment had its own customer, cluster randomisation would be
    identical to payment randomisation and S7 would be untestable."""
    b = generate_batch(n=400, seed=7)
    assert len({d.customer_id for d in b.declines}) < 400


def test_covariate_is_strictly_pre_treatment():
    for d in generate_batch(n=50, seed=7).declines:
        assert d.covariate_asof_ms < d.ts_ms


# ─────────────────────────────────────────────────────────────────────────────
# Success criteria for the slice
# ─────────────────────────────────────────────────────────────────────────────
def test_every_decline_produced_a_decision_and_an_outcome(batch):
    result, path = batch
    conn = connect(path)
    try:
        counts = dict(
            conn.execute("SELECT entry_type, COUNT(*) FROM ledger GROUP BY entry_type").fetchall()
        )
    finally:
        conn.close()
    assert counts["DECISION"] == result.n_declines
    assert counts["OUTCOME"] == result.n_declines


def test_zero_policy_violations(batch):
    """The compliance story. An ACTUATION with no authorising allow must never
    exist."""
    result, path = batch
    assert result.policy_violations == 0

    conn = connect(path)
    try:
        # Every executed actuation must point at a DECISION that OPA allowed.
        orphans = conn.execute(
            "SELECT COUNT(*) FROM ledger a WHERE a.entry_type='ACTUATION' AND a.executed=1 "
            "AND NOT EXISTS (SELECT 1 FROM ledger d WHERE d.seq=a.decision_seq "
            "AND d.entry_type='DECISION' AND d.opa_allow=1)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert orphans == 0


def test_praman_verify_passes_on_the_produced_ledger(batch):
    _, path = batch
    conn = connect(path)
    try:
        ok, broken_at, msg = verify(conn)
    finally:
        conn.close()
    assert ok, f"broken at {broken_at}: {msg}"


def test_holdout_arm_received_no_actuation(batch):
    """The counterfactual baseline is only valid if the holdout was genuinely
    untouched."""
    _, path = batch
    conn = connect(path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type='ACTUATION' AND arm='holdout'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_holdout_still_gets_a_decision_record(batch):
    """We record what we WOULD have done. Without it there is no way to show a
    reviewer that the arms were comparable."""
    _, path = batch
    conn = connect(path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type='DECISION' AND arm='holdout'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n > 0


def test_both_arms_are_populated(batch):
    result, _ = batch
    assert result.n_treatment > 0 and result.n_holdout > 0


def test_every_decision_carries_the_revision_opa_reported(batch):
    _, path = batch
    conn = connect(path)
    try:
        revs = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT bundle_revision FROM ledger WHERE entry_type='DECISION'"
            )
        }
    finally:
        conn.close()
    assert "UNKNOWN" not in revs
    assert revs == {"mockrev00000001"}


def test_counters_come_from_actuations_not_decisions(batch):
    """Law #7. A denied decision is not an attempt."""
    result, path = batch
    conn = connect(path)
    try:
        actuated = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type='ACTUATION' AND executed=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert actuated == result.n_actuated
    assert result.n_actuated < result.n_declines  # some were denied or held out


# ─────────────────────────────────────────────────────────────────────────────
# Measurement over the REAL ledger
# ─────────────────────────────────────────────────────────────────────────────
def test_experiment_loads_out_of_the_ledger(batch):
    result, path = batch
    conn = connect(path)
    try:
        y, treated, cluster, cov = load_experiment(conn, experiment_id="slice-test")
    finally:
        conn.close()
    assert len(y) == len(treated) == len(cluster) == len(cov) == result.n_declines
    assert treated.sum() > 0 and (~treated.astype(bool)).sum() > 0


def test_estimate_has_a_bracketing_interval(batch):
    _, path = batch
    conn = connect(path)
    try:
        est = estimate_from_ledger(conn, experiment_id="slice-test")
    finally:
        conn.close()
    assert est.ci_lo < est.tau_hat < est.ci_hi


def test_estimate_covers_the_sealed_truth(batch):
    """The batch's true ITT effect is computable from the sealed potential
    outcomes. A single batch is one draw, so this is a sanity check rather than
    a coverage claim -- `validate-estimator` is what earns that."""
    result, path = batch
    conn = connect(path)
    try:
        est = estimate_from_ledger(conn, experiment_id="slice-test")
    finally:
        conn.close()
    assert est.ci_lo <= result.true_itt_paise <= est.ci_hi, (
        f"true {result.true_itt_paise:.0f} outside [{est.ci_lo:.0f}, {est.ci_hi:.0f}]"
    )


def test_naive_gross_overstates_against_the_same_batch(batch):
    result, _ = batch
    assert result.naive_gross_paise > result.true_itt_paise


# ─────────────────────────────────────────────────────────────────────────────
# Same path, real OPA
# ─────────────────────────────────────────────────────────────────────────────
def _opa_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8181/health", timeout=1.0).status_code == 200
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _opa_up(), reason="OPA sidecar not running on :8181")
def test_slice_runs_against_live_opa(tmp_path):
    path = tmp_path / "live.db"
    result = run_batch(n=200, seed=1, ledger_path=path, experiment_id="slice-live")
    assert result.policy_violations == 0
    conn = connect(path)
    try:
        assert verify(conn)[0]
    finally:
        conn.close()
