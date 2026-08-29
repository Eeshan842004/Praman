"""Does the model earn its place? The same batch, twice.

The wrong way to justify a model here is "AUC went up". AUC is a property of the
model; it is not a property of the system, and nobody buys a ranking statistic.
The kernel consumes exactly one number from attribution -- `max_posterior`,
tested against a confidence floor in Rego -- so better attribution pays off in
one specific way: declines that were too uncertain to act on become legal to act
on, and some of those recover money.

So the comparison is run at the level where it matters:

    share of declines terminating at T0
    share blocked by the confidence floor
    actuations executed
    incremental recovery, with its interval

Identical batch, identical seeds, identical policy bundle, identical arm
assignment. Only the attribution differs, so any difference in the four numbers
above is attributable to the model and to nothing else. That is what makes this
an ablation rather than two runs that happen to be near each other.

The honest possibility is built in: the answer may be that the model does NOT
earn its place. The taxonomy heuristic is already the exact Bayes posterior
given (symbol, side signals), so a learned model has to beat an analytically
correct baseline using extra features -- and it may not. Reporting that outcome
is the same work as reporting the other one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from praman.attribution.bayes import heuristic_posterior, information_report
from praman.attribution.featurize import to_frame
from praman.kernel.opa_client import PolicyClient
from praman.ledger.chain import connect
from praman.measure.assign import DEFAULT_HOLDOUT_PCT
from praman.measure.from_ledger import load_experiment
from praman.measure.harness import Estimate, estimate_ate
from praman.sim.generator import DeclineBatch
from praman.slice_runner import RunResult, run_batch


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


@dataclass(slots=True)
class ArmSummary:
    label: str
    result: RunResult
    icr: float

    @property
    def t0_share(self) -> float:
        return _pct(self.result.tier_counts.get("T0", 0), self.result.n_declines)

    @property
    def low_confidence_share(self) -> float:
        return _pct(self.result.low_confidence_declines, self.result.n_declines)

    @property
    def tau(self) -> float:
        return self.result.estimate.tau_hat if self.result.estimate else 0.0


def paired_delta(
    heuristic_ledger: Path,
    model_ledger: Path,
    experiment_id: str,
    n_boot: int = 2000,
    seed: int = 0,
) -> Estimate:
    """A confidence interval on the DIFFERENCE between the two attributions.

    Quoting "worth Rs Y" from two bare point estimates would be the exact thing
    this project criticises incumbents for. The two runs share a batch, a seed
    and an arm assignment, so they are paired and the difference is far better
    determined than either estimate alone -- but "better determined" is not
    "known", and it still needs an interval.

    The identity that makes this clean:

        tau_ml - tau_heur = ATE computed on d_i = y_ml,i - y_heur,i

    because the arm means are linear. So the delta is estimated by the SAME
    customer-level cluster bootstrap, with the same CUPED adjustment, applied to
    the per-payment difference. Holdout rows contribute d = 0 by construction --
    neither run acts on them -- which is what makes the pairing exact rather
    than approximate.
    """
    h_conn, m_conn = connect(heuristic_ledger), connect(model_ledger)
    try:
        y_h, treated_h, cluster_h, covariate = load_experiment(h_conn, experiment_id)
        y_m, treated_m, cluster_m, _ = load_experiment(m_conn, experiment_id)
    finally:
        h_conn.close()
        m_conn.close()

    if not np.array_equal(cluster_h, cluster_m) or not np.array_equal(treated_h, treated_m):
        raise ValueError(
            "the two runs are not paired -- same batch, seed and experiment_id required"
        )

    return estimate_ate(y_m - y_h, treated_h, cluster_h, covariate, n_boot=n_boot, seed=seed)


@dataclass(slots=True)
class Ablation:
    heuristic: ArmSummary
    model: ArmSummary
    n: int
    gate_auc: float
    gate_min_auc: float
    delta: Estimate | None = None

    @property
    def moved_from_terminate(self) -> float:
        """Percentage points of declines moved out of T0 by the model."""
        return self.heuristic.t0_share - self.model.t0_share

    @property
    def extra_actuations(self) -> int:
        return self.model.result.n_actuated - self.heuristic.result.n_actuated

    @property
    def delta_tau(self) -> float:
        return self.model.tau - self.heuristic.tau

    @property
    def worth_paise(self) -> float:
        """Incremental rupees per decline x declines. The sentence a merchant
        can act on, rather than a ranking statistic."""
        return self.delta_tau * self.n

    @property
    def delta_excludes_zero(self) -> bool:
        """Did the recovery difference separate from zero at all?"""
        if self.delta is None:
            return False
        return self.delta.ci_lo > 0 or self.delta.ci_hi < 0

    @property
    def buys_confidence_not_accuracy(self) -> bool:
        """The specific failure worth naming out loud.

        A model can act MORE often while knowing LESS: sharper posteriors clear
        the Rego confidence floor more easily, so more tiers become legal, while
        a lower ICR says those posteriors are worse predictions of the true
        cause. That is S5 -- a legal action on a false premise -- arriving
        through the one number the kernel actually reads.
        """
        return (
            self.model.icr < self.heuristic.icr
            and self.model.low_confidence_share < self.heuristic.low_confidence_share
        )

    @property
    def model_earns_its_place(self) -> bool:
        """Measured, and the AUC gate is only the first of three conditions.

        A model can rank well and still be worse where it counts. It must also
        capture more information than the analytically-correct heuristic it
        would replace, and the recovery difference must actually separate from
        zero -- otherwise "worth Rs Y" is a point estimate with no interval,
        which is precisely what this project argues against.
        """
        return (
            self.gate_auc >= self.gate_min_auc
            and self.model.icr > self.heuristic.icr
            and self.delta_excludes_zero
        )

    def render(self) -> str:
        w = 74
        h, m = self.heuristic, self.model
        lines = [
            "=" * w,
            f"ABLATION . the same {self.n:,} declines, attribution swapped",
            "=" * w,
            "  identical batch, seeds, bundle and arm assignment -- only the",
            "  attribution differs, so every delta below is the model's doing.",
            "",
            f"  {'':28} {'heuristic':>12} {'ML':>12} {'delta':>12}",
            f"  {'terminating at T0':28} {h.t0_share:>11.1f}% {m.t0_share:>11.1f}%"
            f" {m.t0_share - h.t0_share:>+11.1f}pp",
            f"  {'blocked by conf. floor':28} {h.low_confidence_share:>11.1f}%"
            f" {m.low_confidence_share:>11.1f}%"
            f" {m.low_confidence_share - h.low_confidence_share:>+11.1f}pp",
            f"  {'actuations executed':28} {h.result.n_actuated:>12,}"
            f" {m.result.n_actuated:>12,} {self.extra_actuations:>+12,}",
            f"  {'incremental per decline':28} {h.tau / 100:>11,.2f} {m.tau / 100:>11,.2f}"
            f" {self.delta_tau / 100:>+11,.2f}",
            f"  {'information capture (ICR)':28} {h.icr:>12.4f} {m.icr:>12.4f}"
            f" {m.icr - h.icr:>+12.4f}",
            "",
            f"  held-out macro AUC .........  {self.gate_auc:.4f}"
            f"   (gate {self.gate_min_auc}: "
            f"{'PASS' if self.gate_auc >= self.gate_min_auc else 'FAIL'})",
        ]
        if self.delta is not None:
            lines += [
                f"  recovery delta (paired) ....  Rs {self.delta.tau_hat / 100:,.2f}"
                f"   95% CI [Rs {self.delta.ci_lo / 100:,.2f}, "
                f"Rs {self.delta.ci_hi / 100:,.2f}]",
                f"  separates from zero ........  {'YES' if self.delta_excludes_zero else 'NO'}",
            ]
        lines.append("-" * w)

        if self.model_earns_its_place:
            lines += [
                f"  VERDICT: ship the model. It moved {self.moved_from_terminate:.1f}% of",
                "  declines from terminate into a legal action, worth "
                f"Rs {self.worth_paise / 100:,.0f} across this batch.",
            ]
        else:
            lines += [
                "  VERDICT: ship the HEURISTIC.",
                f"  AUC {self.gate_auc:.4f} clears the gate, but the gate is necessary and",
                "  not sufficient. Measured where the kernel actually consumes",
                "  attribution, the model does not earn its place:",
            ]
            if self.model.icr <= self.heuristic.icr:
                lines.append(f"    - it captures LESS information ({m.icr:.4f} vs {h.icr:.4f})")
            if not self.delta_excludes_zero:
                lines.append("    - the recovery difference does not separate from zero")
            if self.buys_confidence_not_accuracy:
                gap = self.heuristic.low_confidence_share - self.model.low_confidence_share
                lines += [
                    "",
                    f"  Read those rows together. The model acts MORE often ({gap:.1f}pp",
                    "  fewer confidence-floor blocks) while capturing LESS information.",
                    "  It is buying actions with confidence rather than with accuracy --",
                    "  S5, a legal action on a false premise, arriving through the exact",
                    "  number the kernel reads. The confidence floor exists to stop that,",
                    "  and a sharper wrong posterior walks straight through it.",
                ]
            lines += [
                "  A model that adds nothing is not free: it adds a training step, a",
                "  serialised artifact, a version to audit, and a second thing that",
                "  can silently go wrong.",
            ]
        lines.append("=" * w)
        return "\n".join(lines)


def run_ablation(
    batch: DeclineBatch,
    model: Any,
    ledger_dir: Path,
    client_factory: Any,
    experiment_prefix: str = "ablation",
    holdout_pct: int = DEFAULT_HOLDOUT_PCT,
    gate_min_auc: float = 0.70,
) -> Ablation:
    """Run the batch under both attributions and score the difference."""
    heur_probs = heuristic_posterior(batch)
    ml_probs = np.asarray(model.predict_proba(to_frame(batch.declines)), dtype=float)

    def _one(label: str, probs: np.ndarray) -> ArmSummary:
        result = run_batch(
            batch=batch,
            n=len(batch.declines),
            ledger_path=ledger_dir / f"{experiment_prefix}-{label}.db",
            client=client_factory(),
            # Same experiment id for both arms, so assign_arm puts every
            # customer in the SAME arm in both runs. A different id per run
            # would re-randomise and the difference would be partly noise.
            experiment_id=experiment_prefix,
            holdout_pct=holdout_pct,
            posteriors=probs,
            attribution_source=label,
        )
        return ArmSummary(label, result, information_report(batch, probs).icr)

    heuristic_arm = _one("heuristic", heur_probs)
    model_arm = _one("ml", ml_probs)

    return Ablation(
        heuristic=heuristic_arm,
        model=model_arm,
        n=len(batch.declines),
        gate_auc=model.metrics.macro_auc,
        gate_min_auc=gate_min_auc,
        delta=paired_delta(
            ledger_dir / f"{experiment_prefix}-heuristic.db",
            ledger_dir / f"{experiment_prefix}-ml.db",
            experiment_prefix,
        ),
    )


def _default_client() -> PolicyClient:
    return PolicyClient()


__all__ = ["Ablation", "ArmSummary", "paired_delta", "run_ablation"]
