"""Canonical taxonomy + likelihood matrix (change C6).

The central claim under test: a decline code does NOT map to one cause. It maps
to a DISTRIBUTION over causes. Code 05 genuinely is ambiguous, and that ambiguity
is the product. A single-cause lookup table would destroy exactly the thing we
are selling.

Written before the implementation exists.
"""

from __future__ import annotations

import math

import pytest

from praman.taxonomy import (
    CAUSES,
    Observation,
    Taxonomy,
    load_taxonomy,
)

TOL = 1e-9


@pytest.fixture(scope="module")
def tax() -> Taxonomy:
    return load_taxonomy()


# ─────────────────────────────────────────────────────────────────────────────
# Structure
# ─────────────────────────────────────────────────────────────────────────────
def test_exactly_nine_canonical_causes():
    assert len(CAUSES) == 9
    assert len(set(CAUSES)) == 9


def test_every_cause_has_class_and_default_tier(tax: Taxonomy):
    for cause in CAUSES:
        meta = tax.cause_meta(cause)
        assert meta.cause_class in {"soft", "hard"}
        assert meta.default_tier in {"T0", "T1", "T2", "T3", "T4"}


def test_hard_causes_default_to_terminal_or_customer_action(tax: Taxonomy):
    """A hard decline must never default to a silent retry."""
    for cause in CAUSES:
        meta = tax.cause_meta(cause)
        if meta.cause_class == "hard":
            assert meta.default_tier in {"T0", "T3", "T4"}, cause


# ─────────────────────────────────────────────────────────────────────────────
# The emission matrix IS the information asymmetry, formalised.
# P(observed symbol | true cause, rail) must be a proper distribution.
# ─────────────────────────────────────────────────────────────────────────────
def test_every_emission_distribution_sums_to_one(tax: Taxonomy):
    for rail in tax.rails():
        for cause in CAUSES:
            dist = tax.emissions(rail, cause)
            if not dist:
                continue
            total = sum(dist.values())
            assert math.isclose(total, 1.0, abs_tol=1e-6), f"{rail}/{cause} sums to {total}"


def test_no_negative_or_above_one_emissions(tax: Taxonomy):
    for rail in tax.rails():
        for cause in CAUSES:
            for symbol, p in tax.emissions(rail, cause).items():
                assert 0.0 <= p <= 1.0, f"{rail}/{cause}/{symbol} = {p}"


@pytest.mark.parametrize(
    ("cause", "symbol", "expected"),
    [
        # Laumans (Adyen): "in approximately half of the cases, 05 Do Not Honor
        # is likely just an Insufficient Funds refusal in disguise."
        ("INSUFFICIENT_FUNDS", "05", 0.50),
        ("INSUFFICIENT_FUNDS", "51", 0.50),
        # Visa remaps certain CNP fraud responses to 05.
        ("ISSUER_RISK_BLOCK", "05", 0.30),
        ("VELOCITY_LIMIT", "05", 0.12),
    ],
)
def test_literature_calibration_anchors(tax: Taxonomy, cause: str, symbol: str, expected: float):
    """These four numbers are load-bearing. Changing one changes the thesis."""
    assert math.isclose(tax.emissions("card", cause)[symbol], expected, abs_tol=TOL)


def test_code_05_is_emitted_by_every_cause(tax: Taxonomy):
    """05 is the catch-all. If any cause cannot emit it, the ambiguity is fake."""
    for cause in CAUSES:
        assert "05" in tax.emissions("card", cause), f"{cause} cannot emit 05"


# ─────────────────────────────────────────────────────────────────────────────
# Likelihood + posterior
# ─────────────────────────────────────────────────────────────────────────────
def _obs(**kw) -> Observation:
    base = {
        "rail": "card",
        "symbol": "05",
        "raw_code": "05",
        "processor_reason": None,
        "source": None,
        "step": None,
        "network_category": None,
        "merchant_advice_code": None,
        "npci_retry_remark": None,
        "cvv_result": None,
        "expiry_valid": None,
        "avs_result": None,
    }
    base.update(kw)
    return Observation(**base)


def test_likelihood_returns_all_nine_causes(tax: Taxonomy):
    lik = tax.likelihood(_obs())
    assert set(lik) == set(CAUSES)
    assert all(v >= 0.0 for v in lik.values())


def test_posterior_is_a_probability_distribution(tax: Taxonomy):
    post = tax.posterior(_obs(), region="IN")
    assert set(post) == set(CAUSES)
    assert math.isclose(sum(post.values()), 1.0, abs_tol=1e-9)
    assert all(0.0 <= v <= 1.0 for v in post.values())


# ── THE PRODUCT THESIS, AS TWO TESTS ────────────────────────────────────────
def test_code_05_yields_an_AMBIGUOUS_posterior(tax: Taxonomy):
    """
    This is the whole pitch. Observing 05 must NOT resolve to one confident cause.
    If this test ever goes green with max > 0.60, we have rebuilt the lookup table
    we set out to replace.
    """
    post = tax.posterior(_obs(symbol="05"), region="IN")
    assert max(post.values()) < 0.60
    plausible = [c for c, p in post.items() if p > 0.05]
    assert len(plausible) >= 3, f"05 collapsed to {plausible}"


def test_npci_Z9_yields_a_SHARP_posterior(tax: Taxonomy):
    """
    The counterpart. NPCI codes carry far more signal than card DE-39 — that is
    the India-first moat. Z9 means insufficient funds and almost nothing else.
    """
    post = tax.posterior(_obs(rail="upi", symbol="Z9", raw_code="Z9"), region="IN")
    assert post["INSUFFICIENT_FUNDS"] > 0.85


def test_card_51_is_sharper_than_card_05(tax: Taxonomy):
    """51 is the honest code; 05 is the catch-all. 51 must carry more information."""
    p05 = tax.posterior(_obs(symbol="05"), region="IN")
    p51 = tax.posterior(_obs(symbol="51"), region="IN")
    assert p51["INSUFFICIENT_FUNDS"] > p05["INSUFFICIENT_FUNDS"]
    assert _entropy(p51) < _entropy(p05)


def _entropy(dist: dict[str, float]) -> float:
    return -sum(p * math.log(p) for p in dist.values() if p > 0)


# ── Side signals (Laumans: use CVC, expiry and AVS as clues) ────────────────
def test_passing_cvv_and_valid_expiry_shifts_05_toward_insufficient_funds(tax: Taxonomy):
    neutral = tax.posterior(_obs(symbol="05"), region="IN")
    clean = tax.posterior(_obs(symbol="05", cvv_result="pass", expiry_valid=True), region="IN")
    assert clean["INSUFFICIENT_FUNDS"] > neutral["INSUFFICIENT_FUNDS"]
    assert clean["EXPIRED_OR_INVALID_CREDENTIAL"] < neutral["EXPIRED_OR_INVALID_CREDENTIAL"]


def test_failing_cvv_shifts_05_away_from_insufficient_funds(tax: Taxonomy):
    neutral = tax.posterior(_obs(symbol="05"), region="IN")
    bad = tax.posterior(_obs(symbol="05", cvv_result="fail"), region="IN")
    assert bad["INSUFFICIENT_FUNDS"] < neutral["INSUFFICIENT_FUNDS"]


# ── Regional priors ─────────────────────────────────────────────────────────
def test_regional_priors_are_distributions(tax: Taxonomy):
    for region in tax.regions():
        prior = tax.prior(region)
        assert set(prior) == set(CAUSES)
        assert math.isclose(sum(prior.values()), 1.0, abs_tol=1e-6)


def test_region_changes_the_posterior(tax: Taxonomy):
    """India's decline mix is not America's. If region is inert, priors are dead code."""
    assert tax.posterior(_obs(symbol="05"), region="IN") != tax.posterior(
        _obs(symbol="05"), region="US"
    )


# ── Robustness: the normaliser must be total ────────────────────────────────
def test_unknown_symbol_does_not_crash_and_stays_uninformative(tax: Taxonomy):
    post = tax.posterior(_obs(symbol="ZZ_NOT_A_REAL_CODE"), region="IN")
    assert math.isclose(sum(post.values()), 1.0, abs_tol=1e-9)
    # With no emission evidence the posterior must fall back to the prior.
    prior = tax.prior("IN")
    for cause in CAUSES:
        assert math.isclose(post[cause], prior[cause], abs_tol=1e-6)


def test_unknown_rail_does_not_crash(tax: Taxonomy):
    post = tax.posterior(_obs(rail="carrier_billing", symbol="05"), region="IN")
    assert math.isclose(sum(post.values()), 1.0, abs_tol=1e-9)
