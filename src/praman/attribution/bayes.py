"""The information ceiling, and the Information Capture Ratio.

We do not claim model accuracy. Labels are synthetic-by-construction, so an
accuracy figure would measure our own generator and nothing about the world.

What IS rigorously answerable -- and only because we authored the generative
model -- is how much of the information that EXISTS in merchant-visible signals
a given predictor actually extracts.

    H(C)      uncertainty from the prior alone
    H(C | X)  the irreducible floor under the true generator
    H_model   cross-entropy the predictor actually achieves

    ICR = (H(C) - H_model) / (H(C) - H(C|X))

Two properties make this a real scale rather than a rhetorical one:

    ICR(Bayes-optimal) == 1.0     the ceiling, by construction
    ICR(prior-only)    == 0.0     the floor, by construction

And H(C|X) has a direct reading: it is the information the ISSUER keeps. A
perfect model still cannot recover it, because it was never sent. That number is
the problem statement, in bits.

This replaces the earlier "94% of theoretically available information" claim,
which divided AUC differences and did not mean what it said.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from praman.sim.generator import DeclineBatch
from praman.taxonomy import CAUSES, Observation, load_taxonomy

_EPS = 1e-12
_LOG2 = np.log(2.0)


def _observation(d) -> Observation:
    return Observation(
        rail=d.rail,
        symbol=d.symbol,
        network_category=d.network_category,
        merchant_advice_code=d.merchant_advice_code,
        npci_retry_remark=d.npci_retry_remark,
        cvv_result=d.cvv_result,
        expiry_valid=d.expiry_valid,
    )


def bayes_posterior(batch: DeclineBatch) -> np.ndarray:
    """P(cause | features, symbol, side signals) -- exactly.

    The generator sampled the cause from P(cause | features), then emitted the
    symbol and side signals conditional on that cause. Multiplying the three
    factors and normalising inverts the generative model precisely, which is
    what makes this a CEILING rather than a strong baseline.
    """
    tax = load_taxonomy()
    out = np.empty((len(batch.declines), len(CAUSES)))

    for i, d in enumerate(batch.declines):
        # likelihood() already combines the emission matrix with the
        # conditionally-independent side-signal channels.
        lik = tax.likelihood(_observation(d))
        row = batch.cause_probs[i] * np.array([lik[c] for c in CAUSES])
        total = row.sum()
        out[i] = row / total if total > 0 else batch.cause_probs[i]

    return out


def heuristic_posterior(batch: DeclineBatch, region: str = "IN") -> np.ndarray:
    """What the taxonomy alone can do: symbol and side signals, no features.

    This is the attribution the vertical slice ships today. The gap between its
    ICR and 1.0 is exactly the room a trained model has to earn.
    """
    tax = load_taxonomy()
    return np.array(
        [[tax.posterior(_observation(d), region=region)[c] for c in CAUSES] for d in batch.declines]
    )


@dataclass(frozen=True, slots=True)
class InformationReport:
    n: int
    h_marginal: float  # H(C), nats
    h_conditional: float  # H(C|X), nats -- the irreducible floor
    h_model: float  # cross-entropy achieved, nats
    icr: float

    @property
    def available_bits(self) -> float:
        """Information merchant-visible signals CAN reveal."""
        return (self.h_marginal - self.h_conditional) / _LOG2

    @property
    def withheld_bits(self) -> float:
        """Information the issuer keeps. Unrecoverable at any model quality."""
        return self.h_conditional / _LOG2

    @property
    def captured_bits(self) -> float:
        return (self.h_marginal - self.h_model) / _LOG2

    def render(self) -> str:
        return "\n".join(
            [
                f"INFORMATION CAPTURE . {self.n:,} declines",
                "-" * 62,
                f"  identifying the cause needs ....  {self.h_marginal / _LOG2:.3f} bits",
                f"  merchant-visible signals hold ..  {self.available_bits:.3f} bits",
                f"  the issuer withholds ...........  {self.withheld_bits:.3f} bits"
                "   <- the problem, measured",
                f"  this model extracts ............  {self.captured_bits:.3f} bits",
                "-" * 62,
                f"  Information Capture Ratio ......  {self.icr:.3f}"
                "   (1.0 = Bayes-optimal, 0.0 = prior-only)",
            ]
        )


def information_report(batch: DeclineBatch, model_probs: np.ndarray) -> InformationReport:
    """Score any predictor on the ICR scale.

    `model_probs` is (n, 9) and rows must sum to 1. Nothing about this is
    specific to LightGBM -- the heuristic posterior, the Bayes posterior and a
    trained model are all scored the same way, which is what makes their numbers
    comparable.
    """
    truth = np.array([CAUSES.index(d.latent_cause) for d in batch.declines])
    rows = np.arange(truth.size)

    counts = np.bincount(truth, minlength=len(CAUSES)).astype(float)
    marginal = counts / counts.sum()
    h_marginal = float(-(marginal * np.log(np.clip(marginal, _EPS, None))).sum())

    # H(C|X): mean entropy of the TRUE posterior. Not estimated -- the generator
    # hands us its own conditional, so this is the exact floor.
    bayes = bayes_posterior(batch)
    h_conditional = float(-(bayes * np.log(np.clip(bayes, _EPS, None))).sum(axis=1).mean())

    h_model = float(-np.log(np.clip(model_probs[rows, truth], _EPS, None)).mean())

    denom = h_marginal - h_conditional
    icr = float((h_marginal - h_model) / denom) if denom > _EPS else 0.0

    return InformationReport(
        n=truth.size,
        h_marginal=h_marginal,
        h_conditional=h_conditional,
        h_model=h_model,
        icr=icr,
    )


__all__ = [
    "InformationReport",
    "bayes_posterior",
    "heuristic_posterior",
    "information_report",
]
