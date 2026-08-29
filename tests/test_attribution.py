"""Phase 4: attribution, and the Information Capture Ratio.

We never claim model accuracy. Labels are synthetic-by-construction, so an
accuracy number would measure our own generator and nothing else.

What IS answerable, because we authored the generative model: how much of the
information that EXISTS in merchant-visible signals does the model actually
extract? Three entropies give it exactly:

    H(C)      what you'd know from the prior alone
    H(C | X)  the irreducible floor under the true generator
              <- the gap H(C) - H(C|X) is precisely what the issuer withholds
    H_model   the cross-entropy our model actually achieves

    ICR = (H(C) - H_model) / (H(C) - H(C|X))

The formula is self-checking, and these tests check it:
    ICR(Bayes-optimal posterior) == 1.0   by construction
    ICR(prior-only predictor)    == 0.0   by construction

Anything in between is a real measurement on a known scale, which an AUC ratio
is not. This also replaces the "94% of theoretically available information"
claim, which was arithmetic on the wrong quantity.

Written before the implementation exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from praman.attribution.bayes import (
    bayes_posterior,
    heuristic_posterior,
    information_report,
)
from praman.sim.generator import generate_batch
from praman.taxonomy import CAUSES


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=8000, seed=21)


# ─────────────────────────────────────────────────────────────────────────────
# The generator must expose its own conditional, or the ceiling is a guess
# ─────────────────────────────────────────────────────────────────────────────
def test_generator_exposes_the_true_cause_probabilities(batch):
    """P(cause | features) is what the tilt-and-correct step actually sampled
    from. Recomputing it approximately would make the 'ceiling' an estimate, and
    then the ratio would no longer be exact."""
    assert batch.cause_probs.shape == (len(batch.declines), len(CAUSES))
    assert np.allclose(batch.cause_probs.sum(axis=1), 1.0)


def test_true_cause_probabilities_actually_predict_the_sampled_cause(batch):
    """Sanity: the sampled causes must be consistent with the distribution they
    were drawn from."""
    picked = np.array([CAUSES.index(d.latent_cause) for d in batch.declines])
    assigned = batch.cause_probs[np.arange(len(picked)), picked]
    assert assigned.mean() > (1.0 / len(CAUSES)) * 1.5


# ─────────────────────────────────────────────────────────────────────────────
# Bayes-optimal posterior
# ─────────────────────────────────────────────────────────────────────────────
def test_bayes_posterior_is_a_distribution(batch):
    p = bayes_posterior(batch)
    assert p.shape == (len(batch.declines), len(CAUSES))
    assert np.allclose(p.sum(axis=1), 1.0)
    assert (p >= 0).all()


def test_bayes_beats_the_heuristic_because_it_sees_more(batch):
    """The heuristic reads only the symbol and side signals. Bayes also reads the
    features. It must win, and the size of the gap is exactly what the trained
    model is being asked to close."""
    truth = np.array([CAUSES.index(d.latent_cause) for d in batch.declines])
    rows = np.arange(len(truth))
    xent_bayes = -np.log(np.clip(bayes_posterior(batch)[rows, truth], 1e-12, None)).mean()
    xent_heur = -np.log(np.clip(heuristic_posterior(batch)[rows, truth], 1e-12, None)).mean()
    assert xent_bayes < xent_heur


# ─────────────────────────────────────────────────────────────────────────────
# ★ The ICR scale: anchored at both ends
# ─────────────────────────────────────────────────────────────────────────────
def test_icr_of_the_bayes_posterior_is_one(batch):
    """By construction. If this drifts, the formula is wrong -- not the model."""
    r = information_report(batch, bayes_posterior(batch))
    assert 0.93 <= r.icr <= 1.07, f"ICR(Bayes) = {r.icr:.4f}"


def test_icr_of_a_prior_only_predictor_is_zero(batch):
    """The other anchor. A predictor that ignores every observation extracts
    none of the available information."""
    n = len(batch.declines)
    counts = np.bincount(
        [CAUSES.index(d.latent_cause) for d in batch.declines], minlength=len(CAUSES)
    )
    prior_only = np.tile(counts / counts.sum(), (n, 1))
    r = information_report(batch, prior_only)
    assert abs(r.icr) < 0.05, f"ICR(prior-only) = {r.icr:.4f}"


def test_conditional_entropy_is_below_marginal_entropy(batch):
    """Observations must reduce uncertainty, or there is nothing to extract."""
    r = information_report(batch, bayes_posterior(batch))
    assert r.h_conditional < r.h_marginal


def test_withheld_information_is_reported_in_bits(batch):
    """H(C|X) is what stays hidden even with a perfect model -- the issuer's
    private information, measured. That number IS the problem statement."""
    r = information_report(batch, bayes_posterior(batch))
    assert r.withheld_bits > 0
    assert r.available_bits > 0
    assert np.isclose(r.available_bits + r.withheld_bits, r.h_marginal / np.log(2), atol=1e-6)


def test_heuristic_icr_is_strictly_between_the_anchors(batch):
    """The taxonomy posterior is real but partial: it never sees the features."""
    r = information_report(batch, heuristic_posterior(batch))
    assert 0.0 < r.icr < 1.0, f"heuristic ICR = {r.icr:.4f}"


def test_report_renders_without_claiming_accuracy(batch):
    text = information_report(batch, heuristic_posterior(batch)).render()
    assert "accuracy" not in text.lower()
    assert "bits" in text.lower()
