"""The ICR ceiling audit — which X does H(C|X) condition on?

The concern this answers: the taxonomy heuristic sees {symbol, side signals,
rail, region}. LightGBM additionally sees the transaction features, and the
simulator conditions the cause ON those features. If the ICR denominator were
computed with the smaller information set, it would be too small, both ratios
would be inflated, and the comparison would be unfair to the model that uses
the extra information. That is the same class of error as the `upi_autopay`
rail bug, so it gets the same treatment: computed, not argued.

Everything here is exact rather than estimated, because symbol and the side
signals are emitted conditional on the cause alone. That conditional
independence is what makes both posteriors closed-form:

    P(C | symbol, side, rail)            = pi          x lik / Z
    P(C | symbol, side, rail, features)  = cause_probs x lik / Z

with `cause_probs` the generator's own P(C | features) and `pi` its column mean.

Written before the implementation exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from praman.attribution.bayes import (
    bayes_posterior,
    entropy_decomposition,
    heuristic_posterior,
    information_report,
)
from praman.sim.generator import generate_batch


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=6000, seed=101)


@pytest.fixture(scope="module")
def decomp(batch):
    return entropy_decomposition(batch)


# ─────────────────────────────────────────────────────────────────────────────
# Information never hurts
# ─────────────────────────────────────────────────────────────────────────────
def test_conditioning_on_more_never_increases_entropy(decomp):
    """The data-processing ordering. A violation here would mean one of the
    posteriors is not the conditional it claims to be."""
    assert decomp.h_marginal >= decomp.h_given_features
    assert decomp.h_marginal >= decomp.h_given_symbol
    assert decomp.h_given_symbol >= decomp.h_given_full
    assert decomp.h_given_features >= decomp.h_given_full


def test_every_mutual_information_is_non_negative(decomp):
    assert decomp.mi_features_bits >= 0
    assert decomp.mi_symbol_bits >= 0
    assert decomp.mi_total_bits >= 0


# ─────────────────────────────────────────────────────────────────────────────
# THE AUDIT QUESTION
# ─────────────────────────────────────────────────────────────────────────────
def test_the_shipped_denominator_uses_the_FULL_information_set(batch, decomp):
    """The finding.

    `information_report` derives H(C|X) from `bayes_posterior`, which multiplies
    the generator's own P(C | features) by the symbol/side likelihood. So the
    denominator already conditions on features AND symbol AND side -- it is the
    full ceiling, and both ICRs are scored against it.

    Had it used the smaller set, every ICR in the project would have been
    inflated and the model that uses features would have been judged unfairly.
    """
    shipped = (
        -(bayes_posterior(batch) * np.log(np.clip(bayes_posterior(batch), 1e-12, None)))
        .sum(axis=1)
        .mean()
    )

    assert shipped == pytest.approx(decomp.h_given_full, abs=1e-9)
    assert shipped != pytest.approx(decomp.h_given_symbol, abs=1e-6)


def test_features_carry_real_but_small_information(decomp):
    """Both halves matter.

    Real: the simulator conditions the cause on month position, velocity, outage
    windows and amount deviation, so a denominator ignoring them would genuinely
    be wrong. Small: the emission matrix is sharp, so once the decline code is
    known there is little left for the features to explain.
    """
    assert decomp.mi_features_bits > 0.1, "features must carry real signal"
    assert 0.0 < decomp.features_share < 0.25, (
        "if features carried most of the information, a features-blind baseline "
        "could not possibly be competitive and Gate 1 would read differently"
    )


# ─────────────────────────────────────────────────────────────────────────────
# The features-blind ceiling
# ─────────────────────────────────────────────────────────────────────────────
def test_a_features_blind_predictor_has_a_ceiling_below_one(decomp):
    """The number that explains Gate 1.

    A predictor that never sees the features cannot reach ICR 1.0 no matter how
    good it is, because part of the available information is in the features.
    Its maximum is I(C; symbol, side) / I(C; everything).
    """
    assert 0.0 < decomp.features_blind_ceiling < 1.0
    assert decomp.features_blind_ceiling + decomp.features_share == pytest.approx(1.0)


def test_the_heuristic_essentially_achieves_its_own_ceiling(batch, decomp):
    """It is not a rule of thumb. It is the exact Bayes posterior for its
    information set, so it should land at the features-blind ceiling -- and the
    small gap to 1.0 is the features it cannot see, not a defect."""
    icr = information_report(batch, heuristic_posterior(batch)).icr
    assert icr == pytest.approx(decomp.features_blind_ceiling, abs=0.02)
    assert icr <= decomp.features_blind_ceiling + 0.02


def test_the_bayes_posterior_reaches_the_ceiling(batch):
    icr = information_report(batch, bayes_posterior(batch)).icr
    assert icr == pytest.approx(1.0, abs=0.03)


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────
def test_render_names_the_conditioning_set(decomp):
    out = decomp.render()
    assert "symbol, side, region, features" in out
    assert "features-blind ceiling" in out


def test_decomposition_is_deterministic():
    a = entropy_decomposition(generate_batch(n=1200, seed=5))
    b = entropy_decomposition(generate_batch(n=1200, seed=5))
    assert a.h_given_full == b.h_given_full
    assert a.features_blind_ceiling == b.features_blind_ceiling


def test_the_ceiling_is_exactly_the_entropy_derived_ratio(decomp):
    """The reconciliation check.

    A reader with a calculator must be able to take the three entropies printed
    in the audit table and arrive at the printed ceiling:

        ceiling = (H(C) - H(C|symbol,side,region)) / (H(C) - H(C|everything))

    If those ever disagree, either the ceiling is stale or a table row is
    mislabelled -- and both are the kind of arithmetic slip that discredits a
    section whose entire subject is measuring things properly.
    """
    derived = (decomp.h_marginal - decomp.h_given_symbol) / (
        decomp.h_marginal - decomp.h_given_full
    )
    assert decomp.features_blind_ceiling == pytest.approx(derived, abs=1e-12)


def test_the_ceiling_moves_with_the_batch_it_was_measured_on():
    """Two batches give two ceilings, and neither is wrong.

    H(C|X) is an average over the rows in hand, so a different sample gives a
    slightly different conditional and therefore a slightly different ceiling.
    This is asserted rather than assumed because quoting a ceiling from one
    batch beside an entropy table from another is exactly how the published
    figures failed to reconcile: 0.9667 from n=5,000 seed=77 against a table
    from n=20,000 seed=101 whose rows imply 0.9611.

    Any figure derived from a ceiling has to name the batch it came from.
    """
    a = entropy_decomposition(generate_batch(n=4000, seed=77))
    b = entropy_decomposition(generate_batch(n=4000, seed=101))
    assert a.features_blind_ceiling != b.features_blind_ceiling
    # Same order of magnitude, though -- they are estimates of one quantity.
    assert abs(a.features_blind_ceiling - b.features_blind_ceiling) < 0.05
