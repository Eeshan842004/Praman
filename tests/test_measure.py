"""Deterministic assignment + the estimator validation harness (C1, C5, S7).

Phase 7 pulled forward deliberately. The estimator's interface is
(Y, W, cluster_id, covariate) -> (tau_hat, ci_lo, ci_hi); it does not need the
payments simulator, so its correctness can be proven now against a toy world
with a known ATE. If CI coverage is wrong, that is a statistics bug, and it is
far cheaper to find today than on Day 7.

The headline assertion is coverage in [0.92, 0.97]. Coverage is a genuinely hard
test to pass: it fails if the interval is too narrow (overconfident) OR too wide
(useless), and it is the reason we can claim the uncertainty is real rather than
decorative.

Written before the implementation exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from praman.measure.assign import assign_arm, bucket
from praman.measure.harness import (
    estimate_ate,
    naive_gross_estimate,
    toy_world,
    validate_estimator,
)


# ─────────────────────────────────────────────────────────────────────────────
# C5 / S7 — deterministic CLUSTER randomisation
# ─────────────────────────────────────────────────────────────────────────────
def test_assignment_is_pure_and_reproducible():
    """No seed state. Same inputs, same arm, on any machine, forever -- which is
    what lets an auditor re-derive every arm from the ledger alone."""
    a = [assign_arm("exp-1", f"cust_{i}") for i in range(500)]
    b = [assign_arm("exp-1", f"cust_{i}") for i in range(500)]
    assert a == b


def test_every_payment_of_a_customer_shares_one_arm():
    """S7. Payment-level randomisation violates SUTVA in subscriptions: a nudge
    on customer A's payment #1 tops up their wallet and recovers payment #2,
    which may sit in the holdout. Cluster at the customer."""
    for cust in ("cust_0007", "cust_0192", "cust_9999"):
        arms = {assign_arm("exp-1", cust) for _ in range(50)}
        assert len(arms) == 1


def test_holdout_share_is_close_to_requested():
    n = 20_000
    arms = [assign_arm("exp-1", f"cust_{i}", holdout_pct=10) for i in range(n)]
    share = arms.count("holdout") / n
    assert 0.09 < share < 0.11, share


@pytest.mark.parametrize("pct", [5, 10, 20, 50])
def test_holdout_share_tracks_the_parameter(pct: int):
    n = 20_000
    arms = [assign_arm("exp-1", f"c{i}", holdout_pct=pct) for i in range(n)]
    assert abs(arms.count("holdout") / n - pct / 100) < 0.015


def test_changing_experiment_id_rerandomises():
    a = [assign_arm("exp-1", f"c{i}") for i in range(2000)]
    b = [assign_arm("exp-2", f"c{i}") for i in range(2000)]
    assert a != b
    disagreement = sum(x != y for x, y in zip(a, b, strict=True)) / len(a)
    assert disagreement > 0.05


def test_bucket_is_uniform_over_the_range():
    vals = np.array([bucket("exp-1", f"c{i}") for i in range(20_000)])
    assert vals.min() >= 0
    assert vals.max() < 10_000
    # Ten equal-width bins should each hold roughly a tenth of the mass.
    counts, _ = np.histogram(vals, bins=10, range=(0, 10_000))
    assert counts.min() > 0.085 * len(vals), counts


def test_assignment_does_not_correlate_with_customer_id_ordering():
    """A hash that leaks ordinal structure would confound the experiment."""
    arms = np.array([assign_arm("exp-1", f"cust_{i:06d}") == "holdout" for i in range(10_000)])
    first_half, second_half = arms[:5000].mean(), arms[5000:].mean()
    assert abs(first_half - second_half) < 0.02


# ─────────────────────────────────────────────────────────────────────────────
# The toy world: sealed potential outcomes with a KNOWN ATE
# ─────────────────────────────────────────────────────────────────────────────
def test_toy_world_is_deterministic():
    a, b = toy_world(7), toy_world(7)
    assert np.array_equal(a.y0, b.y0)
    assert np.array_equal(a.y1, b.y1)
    assert a.true_ate == b.true_ate


def test_toy_world_has_intra_cluster_correlation():
    """Without clustering the harness would not test what it claims to test:
    a naive iid bootstrap only fails when units inside a cluster are correlated."""
    w = toy_world(1)
    overall = w.y0.var()
    within = np.mean([w.y0[w.cluster_id == k].var() for k in np.unique(w.cluster_id)])
    assert within < 0.9 * overall, "clusters carry no signal; ICC is ~0"


def test_toy_world_covariate_is_predictive_and_pre_treatment():
    w = toy_world(2)
    r = np.corrcoef(w.covariate, w.y0)[0, 1]
    assert r > 0.5, f"covariate barely predicts the outcome (r={r:.2f}); CUPED cannot help"


def test_toy_world_true_ate_matches_its_potential_outcomes():
    w = toy_world(3)
    assert np.isclose(w.true_ate, float((w.y1 - w.y0).mean()))


# ─────────────────────────────────────────────────────────────────────────────
# The estimator
# ─────────────────────────────────────────────────────────────────────────────
def _observe(w, holdout_pct: int = 10):
    treat = np.array(
        [assign_arm("exp-1", f"cust_{k}", holdout_pct) == "treatment" for k in w.cluster_id]
    )
    y = np.where(treat, w.y1, w.y0)
    return y, treat.astype(int)


def test_estimate_returns_a_bracketing_interval():
    w = toy_world(11)
    y, arm = _observe(w)
    est = estimate_ate(y, arm, w.cluster_id, w.covariate)
    assert est.ci_lo < est.tau_hat < est.ci_hi


def test_cuped_reduces_interval_width():
    """If the covariate does not shrink the interval, CUPED is dead code."""
    w = toy_world(13)
    y, arm = _observe(w)
    plain = estimate_ate(y, arm, w.cluster_id, covariate=None)
    cuped = estimate_ate(y, arm, w.cluster_id, covariate=w.covariate)
    assert (cuped.ci_hi - cuped.ci_lo) < (plain.ci_hi - plain.ci_lo)
    assert cuped.variance_reduction > 0.10


def test_cluster_bootstrap_is_wider_than_pretending_units_are_independent():
    """Resampling payments instead of customers understates variance whenever
    cluster sizes vary -- which they always do."""
    w = toy_world(17)
    y, arm = _observe(w)
    clustered = estimate_ate(y, arm, w.cluster_id, w.covariate)
    iid = estimate_ate(y, arm, np.arange(y.size), w.covariate)  # every unit its own cluster
    assert (clustered.ci_hi - clustered.ci_lo) > (iid.ci_hi - iid.ci_lo)


def test_naive_gross_estimator_overstates_the_effect():
    """The number the industry reports: total recovered, no holdout. It counts
    every unit that would have recovered on its own."""
    w = toy_world(19)
    y, arm = _observe(w)
    naive = naive_gross_estimate(y, arm)
    honest = estimate_ate(y, arm, w.cluster_id, w.covariate)
    assert naive > honest.tau_hat
    assert naive > w.true_ate


# ─────────────────────────────────────────────────────────────────────────────
# ★ THE HEADLINE: does the interval actually cover 95% of the time?
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow
def test_ci_coverage_is_nominal_across_200_worlds():
    """
    200 worlds, each with a sealed true ATE the estimator never sees.

    Coverage must land in [0.92, 0.97]. Too low and the interval is
    overconfident; too high and it is uninformative. This assertion failing
    means the uncertainty quantification is wrong -- and nothing downstream of
    it can be believed.
    """
    report = validate_estimator(n_worlds=200, holdout_pct=10, n_boot=1500, seed0=9000)

    assert 0.92 <= report.coverage <= 0.97, f"coverage {report.coverage:.3f}"
    assert abs(report.mean_bias_pct) < 5.0, f"bias {report.mean_bias_pct:.2f}%"
    assert report.mean_variance_reduction > 0.05


@pytest.mark.slow
def test_naive_estimator_is_badly_biased_and_never_covers():
    """The contrast that carries the pitch."""
    report = validate_estimator(n_worlds=200, holdout_pct=10, n_boot=500, seed0=9000)
    assert report.naive_mean_bias_pct > 25.0
    assert report.naive_coverage < 0.10
