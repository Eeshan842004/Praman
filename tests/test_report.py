"""The three-tier result.

The ordering IS the argument, so the tests defend the ordering and the wording
as much as the arithmetic. Every number an incumbent reports lives in our third
tier, without an interval; if the tiers were reordered, or the illustrative run
quietly dropped because it looks weak, the claim would collapse back into the
rupee figure this restructure exists to replace.
"""

from __future__ import annotations

from pathlib import Path

from praman.measure.harness import Estimate, ValidationReport
from praman.measure.report import ThreeTierReport
from praman.slice_runner import RunResult


def _validation(coverage: float = 0.945, naive_bias: float = 57.0) -> ValidationReport:
    return ValidationReport(
        n_worlds=200,
        holdout_pct=20,
        coverage=coverage,
        mean_bias_pct=-0.3,
        rmse=1400.0,
        mean_ci_width=5000.0,
        mean_variance_reduction=0.39,
        naive_mean_bias_pct=naive_bias,
        naive_coverage=0.0,
        mean_true_ate=3500.0,
    )


def _estimate(tau: float, lo: float, hi: float) -> Estimate:
    return Estimate(
        tau_hat=tau,
        ci_lo=lo,
        ci_hi=hi,
        variance_reduction=0.39,
        n_treatment=100,
        n_holdout=25,
        n_clusters_treatment=80,
        n_clusters_holdout=20,
        se=(hi - lo) / 3.92,
    )


def _run(n: int, tau: float, lo: float, hi: float, truth: float, naive: float) -> RunResult:
    return RunResult(
        experiment_id="t",
        ledger_path=Path("x"),
        n_declines=n,
        n_actuated=n // 2,
        true_itt_paise=truth,
        naive_gross_paise=naive,
        estimate=_estimate(tau, lo, hi),
    )


def _full() -> ThreeTierReport:
    return ThreeTierReport(
        primary=_validation(),
        secondary=_run(14000, 3400.0, 900.0, 5900.0, 3510.0, 8839.0),
        illustrative=_run(3000, 2048.0, -4076.0, 6234.0, 3510.0, 8839.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ordering is the argument
# ─────────────────────────────────────────────────────────────────────────────
def test_primary_comes_first():
    out = _full().render()
    assert out.index("PRIMARY") < out.index("SECONDARY") < out.index("ILLUSTRATIVE")


def test_the_headline_is_the_estimator_not_a_rupee_figure():
    out = _full().render()
    assert "the estimator, not the outcome" in out
    assert "We do not claim recovered revenue" in out


def test_primary_shows_coverage_beside_the_naive_estimator():
    out = _full().render()
    assert "95% CI coverage" in out
    assert "94.5%" in out
    assert "+57.0%" in out
    assert "what incumbents report" in out


# ─────────────────────────────────────────────────────────────────────────────
# Secondary must not overclaim
# ─────────────────────────────────────────────────────────────────────────────
def test_secondary_reports_whether_the_interval_excludes_zero():
    out = _full().render()
    assert "excludes zero .............  YES" in out


def test_secondary_says_NO_when_the_interval_straddles_zero():
    """The field is a measurement, not a decoration. A powered run that still
    straddles zero has to say so."""
    r = ThreeTierReport(secondary=_run(14000, 100.0, -500.0, 700.0, 350.0, 900.0))
    assert "excludes zero .............  NO" in r.render()


def test_secondary_reports_coverage_of_the_sealed_truth():
    assert "covered by the interval ...  YES" in _full().render()


# ─────────────────────────────────────────────────────────────────────────────
# Illustrative is kept ON PURPOSE
# ─────────────────────────────────────────────────────────────────────────────
def test_illustrative_is_labelled_as_deliberate():
    assert "kept on purpose" in _full().render()


def test_illustrative_shows_the_naive_estimate_missing_the_truth():
    """The entire contrast: our interval is wide and contains the answer; the
    naive number is precise, has no interval, and is outside ours."""
    out = _full().render()
    assert "inside our interval .......  NO" in out
    assert "no counterfactual" in out
    assert "Honest and wide beats confident and wrong." in out


def test_illustrative_quantifies_how_wrong_the_naive_number_is():
    out = _full().render()
    # (8839 - 3510) / 3510 = +152%
    assert "+152%" in out


# ─────────────────────────────────────────────────────────────────────────────
# Partial reports degrade cleanly
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_tiers_are_omitted_not_faked():
    assert ThreeTierReport().render() == ""
    only_primary = ThreeTierReport(primary=_validation()).render()
    assert "PRIMARY" in only_primary
    assert "SECONDARY" not in only_primary


def test_a_run_without_an_estimate_renders_nothing_for_that_tier():
    r = ThreeTierReport(secondary=RunResult(experiment_id="t", ledger_path=Path("x")))
    assert "SECONDARY" not in r.render()
