"""Power analysis.

The point of this module is to move "the interval straddles zero" from a
discovery into a prediction. So the tests check two different things:

  * the arithmetic is right (MDE, the inverse that picks n, the rounding
    direction), and
  * the measurement is stable enough to be worth inverting.

The second is what actually failed in practice. At two runs per grid point the
measured SE at n=3,000 came out LARGER than at n=2,000, and a line fitted
through those points disagreed with the measured point by 55% at exactly the n
the headline claim rests on. A power curve reporting its own sampling noise is
worse than no power curve, because it looks like an answer.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pytest
from scipy.stats import norm
from tests.conftest import rego_like_client

from praman.measure.power import (
    ALPHA,
    MAX_RUNS,
    MIN_RUNS,
    POWER,
    PowerCurve,
    PowerPoint,
    mde,
    power_curve,
    runs_for,
)
from praman.sim.generator import RECOVERY_RATES


# ─────────────────────────────────────────────────────────────────────────────
# The effect size is not a tuning knob
# ─────────────────────────────────────────────────────────────────────────────
def test_the_effect_size_is_frozen():
    """Powering an experiment means choosing n. It never means choosing tau.

    Moving the recovery rates until the interval excludes zero would tune the
    generator to flatter the product, and every number downstream would be
    measuring that choice instead of the estimator. This hash is here so that
    edit cannot happen quietly -- if it is deliberate it must be deliberate in
    the diff of this test too.
    """
    digest = hashlib.sha256(
        json.dumps({k: list(v) for k, v in sorted(RECOVERY_RATES.items())}).encode()
    ).hexdigest()
    assert digest == "49a071c4d34ea4db272d0aeafccad84ca06f3291973a667466db846e1317e506", (
        "RECOVERY_RATES changed. If this is intentional, every power and "
        "measurement number in the repo is stale and must be re-derived."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Arithmetic
# ─────────────────────────────────────────────────────────────────────────────
def test_mde_matches_the_closed_form():
    z = norm.ppf(1 - ALPHA / 2) + norm.ppf(POWER)
    assert mde(100.0) == pytest.approx(z * 100.0)
    assert mde(0.0) == 0.0


def test_mde_is_linear_in_the_standard_error():
    assert mde(200.0) == pytest.approx(2 * mde(100.0))


def test_required_n_fitted_inverts_mde_at():
    """The fitted rule must clear the effect when its own answer is fed back in.

    This is the fit's internal consistency. Which rule actually CHOOSES n is a
    separate question, settled further down in favour of the measured grid.
    """
    curve = PowerCurve(points=[_point(n, 631_00 / math.sqrt(n)) for n in (1000, 4000, 9000)])
    effect = 3_500.0
    n = curve.required_n_fitted(effect)
    assert curve.mde_at(n) <= effect


def test_required_n_rounds_up_never_down():
    """Rounding to the nearest thousand would leave the design fractionally
    underpowered, which is the exact failure this computation prevents."""
    curve = PowerCurve(points=[_point(n, 631_00 / math.sqrt(n)) for n in (1000, 4000)])
    effect = 3_500.0
    n = curve.required_n_fitted(effect)
    assert n % 1000 == 0
    assert curve.mde_at(n - 1000) > effect


def test_required_n_is_zero_when_there_is_no_effect_to_detect():
    curve = PowerCurve(points=[_point(1000, 100.0)])
    assert curve.required_n(0.0) == 0


def test_bigger_effects_need_smaller_batches():
    curve = PowerCurve(points=[_point(n, 631_00 / math.sqrt(n)) for n in (1000, 4000)])
    assert curve.required_n_fitted(7_000.0) < curve.required_n_fitted(3_500.0)


# ─────────────────────────────────────────────────────────────────────────────
# Replicate allocation -- the fix for the noisy first curve
# ─────────────────────────────────────────────────────────────────────────────
def test_runs_are_allocated_to_equalise_precision():
    """Small batches are cheap, so they get more replicates. Holding total
    observations roughly constant is what makes adjacent grid points
    comparable."""
    assert runs_for(1_000) > runs_for(8_000)
    assert runs_for(1_000) * 1_000 <= runs_for(8_000) * 8_000 * 4


def test_runs_are_clamped_at_both_ends():
    assert runs_for(10) == MAX_RUNS
    assert runs_for(10_000_000) == MIN_RUNS


# ─────────────────────────────────────────────────────────────────────────────
# The curve, measured
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def measured():
    return power_curve(
        grid=(600, 2_400),
        seeds_per_point=4,
        client_factory=rego_like_client,
    )


def test_standard_error_falls_as_the_batch_grows(measured):
    lo, hi = measured.points
    assert hi.se_paise < lo.se_paise, (
        "SE did not fall with n -- the curve is reporting noise, not scaling"
    )


def test_scaling_is_close_to_the_root_n_law(measured):
    """Theory says -0.5 for a mean over independent clusters. A materially
    different exponent means the bootstrap unit and the randomisation unit have
    come apart, which would invalidate every interval the system reports."""
    assert measured.observed_exponent == pytest.approx(-0.5, abs=0.25)


def test_the_bootstrap_se_agrees_with_the_across_run_spread(measured):
    """An independent check on the interval itself. The bootstrap SE and the
    Monte Carlo SD of tau_hat estimate the same quantity by different routes; if
    they disagreed, no MDE built on the bootstrap would mean anything."""
    for p in measured.points:
        if p.se_mc_paise > 0:
            assert 0.4 < p.bootstrap_agreement < 2.5, (
                f"n={p.n}: bootstrap {p.se_paise:.0f} vs monte carlo {p.se_mc_paise:.0f}"
            )


def test_the_effect_does_not_drift_with_batch_size(measured):
    """The ITT effect is a per-decline mean, so it must not depend on n. If it
    did, the grid would be measuring something other than the effect."""
    effects = [p.itt_paise for p in measured.points]
    assert np.std(effects) < 0.5 * abs(np.mean(effects))


def test_every_point_records_how_many_runs_it_averaged(measured):
    """A curve that hides its replicate count cannot be audited for the exact
    failure that produced the first, unusable version of it."""
    assert all(p.n_runs > 0 for p in measured.points)


def test_render_states_that_the_effect_is_fixed(measured):
    out = measured.render()
    assert "FIXED" in out
    assert "sqrt(n)" in out


# ─────────────────────────────────────────────────────────────────────────────
def _point(n: int, se: float) -> PowerPoint:
    return PowerPoint(
        n=n,
        se_paise=se,
        se_mc_paise=se,
        mde_paise=mde(se),
        itt_paise=3_500.0,
        n_actuated=0,
        n_runs=3,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Choosing n: measured grid over fitted law
# ─────────────────────────────────────────────────────────────────────────────
def _noisy_curve() -> PowerCurve:
    """The shape actually measured: n=3,000 is an outlier the fit smooths away."""
    raw = [(1000, 1849.0), (2000, 1204.0), (3000, 1465.0), (5000, 636.0), (8000, 589.0)]
    return PowerCurve(points=[_point(n, se) for n, se in raw])


def test_the_monotone_rule_ignores_a_single_lucky_grid_point():
    """n=2,000 detects but n=3,000 does not. A first-crossing rule would pick
    2,000 off that one point; requiring every LARGER point to detect too is what
    makes the choice robust to the non-monotonicity we actually measured."""
    curve = _noisy_curve()
    assert curve.required_n_measured(3379.0) == 5000


def test_the_recommendation_takes_the_more_conservative_rule():
    """The fit runs optimistic on a heavy-tailed outcome. When two power rules
    disagree, the only safe direction to be wrong in is more observations."""
    curve = _noisy_curve()
    effect = 3379.0
    assert curve.required_n_fitted(effect) < curve.required_n_measured(effect)
    assert curve.required_n(effect) == max(
        curve.required_n_fitted(effect), curve.required_n_measured(effect)
    )


def test_required_n_measured_is_zero_when_no_grid_point_detects():
    curve = PowerCurve(points=[_point(1000, 5000.0)])
    assert curve.required_n_measured(1.0) == 0


# ─────────────────────────────────────────────────────────────────────────────
# The measurement is an artifact, not a memory
# ─────────────────────────────────────────────────────────────────────────────
def test_a_curve_round_trips_through_json():
    """The curve costs ~20 minutes of live OPA. Reconstructing it by hand for a
    demo would be fabrication, so it has to persist exactly."""
    original = _noisy_curve()
    restored = PowerCurve.from_json(original.to_json())
    assert [p.n for p in restored.points] == [p.n for p in original.points]
    assert [p.se_paise for p in restored.points] == [p.se_paise for p in original.points]
    assert restored.required_n(3379.0) == original.required_n(3379.0)
