"""Phase 3: features must genuinely depend on the latent cause.

This is the test that stops Phase 4 being vacuous.

If features are independent of the latent cause, then the decline SYMBOL is the
only information that exists. Bayes extracts it analytically, LightGBM learns
nothing on top, and the Information Capture Ratio comes out ~1.0 for free --
a headline number that would mean precisely nothing.

So the generator must encode real structure, and this file asserts it:

  INSUFFICIENT_FUNDS  concentrates at month-end, collapses right after payday
  VELOCITY_LIMIT      tracks prior attempt density in a short window
  TECHNICAL_DECLINE   arrives in time-clustered bursts (downtime is bursty,
                      not Poisson)
  ISSUER_RISK_BLOCK   tracks how far the amount deviates from that customer's
                      own history

Each claim is checked against a SHUFFLED control, so none of them can pass by
accident.

Written before the implementation exists.
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.feature_selection import mutual_info_classif

from praman.sim.features import (
    FEATURE_COLUMNS,
    IEEECISFeatureSource,
    SyntheticFeatureSource,
    default_feature_source,
)
from praman.sim.generator import generate_batch
from praman.taxonomy import CAUSES, load_taxonomy


@pytest.fixture(scope="module")
def big():
    return generate_batch(n=6000, seed=11)


def _matrix(batch) -> tuple[np.ndarray, np.ndarray, list[str]]:
    cols = [c for c in FEATURE_COLUMNS if isinstance(getattr(batch.declines[0], c), int | float)]
    x = np.array([[float(getattr(d, c)) for c in cols] for d in batch.declines])
    y = np.array([CAUSES.index(d.latent_cause) for d in batch.declines])
    return x, y, cols


# ─────────────────────────────────────────────────────────────────────────────
# The adapter: Kaggle must never be able to block the build
# ─────────────────────────────────────────────────────────────────────────────
def test_synthetic_source_produces_the_declared_schema():
    rows = SyntheticFeatureSource().sample(100, np.random.default_rng(0))
    assert set(rows) >= {"amount_paise", "hour_of_day", "network", "funding"}
    assert len(rows["amount_paise"]) == 100


def test_default_source_works_without_the_ieee_artifact(monkeypatch):
    """The build must run on a clean clone with no Kaggle credentials."""
    monkeypatch.setattr(IEEECISFeatureSource, "available", staticmethod(lambda: False))
    src = default_feature_source()
    assert isinstance(src, SyntheticFeatureSource)
    assert len(src.sample(10, np.random.default_rng(0))["amount_paise"]) == 10


def test_ieee_source_matches_the_synthetic_schema_when_present():
    """Swapping the source must not move anything downstream."""
    if not IEEECISFeatureSource.available():
        pytest.skip("ieee_features.parquet not built")
    a = IEEECISFeatureSource().sample(50, np.random.default_rng(0))
    b = SyntheticFeatureSource().sample(50, np.random.default_rng(0))
    assert set(a) == set(b)


def test_generator_accepts_an_injected_source():
    b = generate_batch(n=50, seed=3, feature_source=SyntheticFeatureSource())
    assert len(b.declines) == 50


# ─────────────────────────────────────────────────────────────────────────────
# ★ Mutual information: features must actually carry signal about the cause
# ─────────────────────────────────────────────────────────────────────────────
def test_features_carry_nontrivial_information_about_the_latent_cause(big):
    x, y, cols = _matrix(big)
    mi = mutual_info_classif(x, y, random_state=0)
    total = float(mi.sum())
    assert total > 0.15, (
        f"features carry almost nothing: {dict(zip(cols, mi.round(4), strict=True))}"
    )


def test_shuffled_features_carry_almost_none(big):
    """The control. Without it, the assertion above could pass on an artefact of
    the estimator rather than on real structure."""
    x, y, _ = _matrix(big)
    rng = np.random.default_rng(0)
    shuffled = x.copy()
    for j in range(shuffled.shape[1]):
        rng.shuffle(shuffled[:, j])
    assert float(mutual_info_classif(shuffled, y, random_state=0).sum()) < 0.05


@pytest.mark.parametrize(
    ("feature", "cause"),
    [
        ("days_since_payday", "INSUFFICIENT_FUNDS"),
        ("attempts_prior_1h", "VELOCITY_LIMIT"),
        ("in_outage", "TECHNICAL_DECLINE"),
        ("amount_z", "ISSUER_RISK_BLOCK"),
    ],
)
def test_each_required_dependency_is_individually_present(big, feature: str, cause: str):
    """Each named driver must move its own cause, measured one feature at a
    time so a single strong feature cannot mask three dead ones."""
    values = np.array([float(getattr(d, feature)) for d in big.declines])
    is_cause = np.array([d.latent_cause == cause for d in big.declines])
    mi = mutual_info_classif(values.reshape(-1, 1), is_cause.astype(int), random_state=0)[0]
    assert mi > 0.004, f"{feature} carries no information about {cause} (MI={mi:.5f})"


# ─────────────────────────────────────────────────────────────────────────────
# The specific structures, asserted directly
# ─────────────────────────────────────────────────────────────────────────────
def test_insufficient_funds_concentrates_at_month_end(big):
    late = [d for d in big.declines if d.days_since_payday >= 20]
    early = [d for d in big.declines if d.days_since_payday <= 3]
    rate_late = sum(d.latent_cause == "INSUFFICIENT_FUNDS" for d in late) / max(len(late), 1)
    rate_early = sum(d.latent_cause == "INSUFFICIENT_FUNDS" for d in early) / max(len(early), 1)
    assert rate_late > 1.5 * rate_early, f"late={rate_late:.3f} early={rate_early:.3f}"


def test_velocity_limit_rises_with_prior_attempt_density(big):
    hi = [d for d in big.declines if d.attempts_prior_1h >= 3]
    lo = [d for d in big.declines if d.attempts_prior_1h == 0]
    r_hi = sum(d.latent_cause == "VELOCITY_LIMIT" for d in hi) / max(len(hi), 1)
    r_lo = sum(d.latent_cause == "VELOCITY_LIMIT" for d in lo) / max(len(lo), 1)
    assert r_hi > 2 * r_lo, f"hi={r_hi:.3f} lo={r_lo:.3f}"


def test_technical_declines_are_bursty_not_poisson(big):
    """Bank downtime clusters. Under a Poisson process the variance of counts
    per time bucket equals the mean; real outages are strongly over-dispersed,
    and a generator that produced smooth technical declines would be lying about
    the one cause whose whole character is that it arrives all at once."""
    ts = np.array([d.ts_ms for d in big.declines if d.latent_cause == "TECHNICAL_DECLINE"])
    assert ts.size > 50
    edges = np.linspace(min(d.ts_ms for d in big.declines), max(d.ts_ms for d in big.declines), 60)
    counts, _ = np.histogram(ts, bins=edges)
    fano = counts.var() / max(counts.mean(), 1e-9)
    assert fano > 2.0, f"Fano factor {fano:.2f} -- indistinguishable from Poisson"


def test_issuer_risk_block_rises_with_amount_deviation(big):
    odd = [d for d in big.declines if abs(d.amount_z) > 1.5]
    typical = [d for d in big.declines if abs(d.amount_z) < 0.5]
    r_odd = sum(d.latent_cause == "ISSUER_RISK_BLOCK" for d in odd) / max(len(odd), 1)
    r_typ = sum(d.latent_cause == "ISSUER_RISK_BLOCK" for d in typical) / max(len(typical), 1)
    assert r_odd > 1.5 * r_typ, f"odd={r_odd:.3f} typical={r_typ:.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# Adding structure must not break calibration
# ─────────────────────────────────────────────────────────────────────────────
def test_marginal_cause_mix_still_tracks_the_regional_prior(big):
    """Tilting P(cause | features) must not silently rewrite the prior the
    posterior is calibrated against."""
    prior = load_taxonomy().prior("IN")
    observed = {
        c: sum(d.latent_cause == c for d in big.declines) / len(big.declines) for c in CAUSES
    }
    for c in CAUSES:
        assert abs(observed[c] - prior[c]) < 0.06, f"{c}: {observed[c]:.3f} vs prior {prior[c]:.3f}"


def test_code_05_still_resolves_the_way_the_literature_says(big):
    """The Laumans anchor has to survive Phase 3."""
    card05 = [d for d in big.declines if d.rail == "card" and d.symbol == "05"]
    assert len(card05) > 100
    nsf = sum(d.latent_cause == "INSUFFICIENT_FUNDS" for d in card05) / len(card05)
    assert 0.40 < nsf < 0.70, f"P(NSF | 05) = {nsf:.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# CUPED needs a covariate that actually predicts
# ─────────────────────────────────────────────────────────────────────────────
def test_cuped_covariate_predicts_the_outcome(big):
    """The slice measured 0% variance reduction because the covariate was noise.
    A covariate uncorrelated with the outcome makes CUPED a no-op, and the whole
    reason CUPED is here is to make an effect detectable at 1000 declines."""
    cov = np.array([d.cuped_covariate for d in big.declines])
    y0 = np.array([d.amount_paise if d.y0_recovered else 0 for d in big.declines], dtype=float)
    r = np.corrcoef(cov, y0)[0, 1]
    assert r > 0.25, f"covariate barely predicts the outcome (r={r:.3f})"


def test_cuped_covariate_is_strictly_pre_treatment(big):
    for d in big.declines:
        assert d.covariate_asof_ms < d.ts_ms
