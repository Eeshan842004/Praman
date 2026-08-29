"""Power analysis for the recovery experiment.

An underpowered experiment is not a failed experiment. It is a failed
experiment DESIGN, and the only thing that separates the two is whether you
computed the minimum detectable effect before you ran it or discovered it
afterwards in a confidence interval that straddles zero.

    MDE = (z_{1-alpha/2} + z_{power}) * SE(tau_hat)

The standard error is MEASURED, not modelled. The textbook route -- assume an
intra-cluster correlation, assume a design effect, assume normal outcomes --
would produce a number about a textbook design. Payment outcomes are amount x
Bernoulli over a heavy-tailed amount, cluster sizes are unequal, and CUPED
removes an amount of variance that is itself an empirical quantity. So the SE
here is the standard deviation of the estimator's own cluster bootstrap, taken
from real pipeline runs at each grid point.

One thing this module must never be used for: choosing an effect size. The
effect is a property of the simulator's recovery rates, which are fixed. Moving
them until the experiment succeeds would be tuning the generator to flatter the
product, and the resulting number would measure nothing. n is the free variable.
Only n.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.stats import norm

from praman.kernel.opa_client import PolicyClient
from praman.measure.assign import DEFAULT_HOLDOUT_PCT
from praman.slice_runner import run_batch

ALPHA = 0.05
POWER = 0.80

# Enough resolution to see the 1/sqrt(n) scaling hold across an order of
# magnitude without paying for a point that tells us nothing new.
DEFAULT_GRID: tuple[int, ...] = (1_000, 2_000, 3_000, 5_000, 8_000, 12_000)

# Replicates are allocated to hold TOTAL observations roughly constant per grid
# point, so every point is measured to roughly equal precision. Two runs per
# point produced an SE at n=3,000 larger than the SE at n=2,000 -- the curve was
# reporting its own sampling noise, and a fitted line through it disagreed with
# the measured point by 55% at exactly the n the headline claim rests on.
OBSERVATION_BUDGET = 24_000
MIN_RUNS, MAX_RUNS = 3, 12


def runs_for(n: int, budget: int = OBSERVATION_BUDGET) -> int:
    """Replicates at this grid point, for roughly equal precision across the grid."""
    return int(min(MAX_RUNS, max(MIN_RUNS, round(budget / n))))


def mde(se: float, alpha: float = ALPHA, power: float = POWER) -> float:
    """Minimum detectable effect for a two-sided test at the given power."""
    return float((norm.ppf(1 - alpha / 2) + norm.ppf(power)) * se)


@dataclass(frozen=True, slots=True)
class PowerPoint:
    n: int
    # Median of the per-run cluster-bootstrap SEs. Median, not mean: payment
    # outcomes are heavy-tailed, so one world with a few very large recoveries
    # drags a mean far enough to invert the ordering of adjacent grid points.
    se_paise: float
    # Standard deviation of tau_hat ACROSS runs -- a direct Monte Carlo estimate
    # of the estimator's sampling SD that owes nothing to the bootstrap. It is
    # the independent check that the bootstrap SE is honest: if these two
    # disagree, the interval the system reports is wrong and no MDE built on it
    # means anything.
    se_mc_paise: float
    mde_paise: float
    itt_paise: float
    n_actuated: int
    n_runs: int = 0

    @property
    def powered(self) -> bool:
        return self.itt_paise >= self.mde_paise

    @property
    def bootstrap_agreement(self) -> float:
        """se_bootstrap / se_montecarlo. 1.0 is perfect agreement."""
        return self.se_paise / self.se_mc_paise if self.se_mc_paise > 0 else float("nan")


@dataclass(slots=True)
class PowerCurve:
    points: list[PowerPoint] = field(default_factory=list)
    alpha: float = ALPHA
    power: float = POWER

    @property
    def true_effect_paise(self) -> float:
        """The ITT effect per decline, pooled across grid points.

        It is a per-decline mean, so it does not depend on n -- and the spread
        across points is a check on exactly that. If it drifted with n, the
        grid would be measuring something other than the effect.
        """
        return float(np.mean([p.itt_paise for p in self.points])) if self.points else 0.0

    @property
    def scale(self) -> float:
        """k in SE(n) = k / sqrt(n), averaged over the measured points."""
        if not self.points:
            return 0.0
        # Median, for the same reason the per-point SE is a median: one
        # heavy-tailed world should not be able to move the recommended n.
        return float(np.median([p.se_paise * math.sqrt(p.n) for p in self.points]))

    @property
    def observed_exponent(self) -> float:
        """The empirical slope of log SE against log n.

        A diagnostic, not an output. For a mean over independent clusters this
        is -0.5 by theory; a materially different value means the bootstrap unit
        and the randomisation unit have come apart, which would invalidate every
        interval the system reports.
        """
        if len(self.points) < 2:
            return float("nan")
        x = np.log([p.n for p in self.points])
        y = np.log([max(p.se_paise, 1e-12) for p in self.points])
        return float(np.polyfit(x, y, 1)[0])

    def se_at(self, n: int) -> float:
        return self.scale / math.sqrt(n)

    def mde_at(self, n: int) -> float:
        return mde(self.se_at(n), self.alpha, self.power)

    def required_n_fitted(self, effect_paise: float | None = None) -> int:
        """n solved from the fitted 1/sqrt(n) scale, rounded UP to a thousand.

        A cross-check, NOT the answer. Rounding up rather than to nearest
        matters wherever this is used: rounding down would leave the design
        fractionally underpowered, which is the failure the whole computation
        exists to prevent.
        """
        effect = effect_paise if effect_paise is not None else self.true_effect_paise
        if effect <= 0 or self.scale <= 0:
            return 0
        z = norm.ppf(1 - self.alpha / 2) + norm.ppf(self.power)
        return int(math.ceil((z * self.scale / effect) ** 2 / 1000.0) * 1000)

    def required_n_measured(self, effect_paise: float | None = None) -> int:
        """Smallest measured n from which EVERY larger measured batch detects.

        Read off the grid, not off a fit. The 1/sqrt(n) law is asymptotically
        right but converges slowly here: the outcome is amount x Bernoulli over
        a lognormal amount, so the 20% holdout mean is driven by a handful of
        large recoveries and its variance is itself heavy-tailed. At 8-12 runs
        per point the fit still missed the measured MDE at n=3,000 by a third --
        that is the estimator's real behaviour at these sample sizes, not noise
        to be averaged away.

        Requiring every LARGER point to detect too, rather than just the first
        crossing, is what makes this robust to exactly that non-monotonicity. A
        single lucky point cannot select the batch size.
        """
        effect = effect_paise if effect_paise is not None else self.true_effect_paise
        if effect <= 0 or not self.points:
            return 0
        ordered = sorted(self.points, key=lambda p: p.n)
        for i, point in enumerate(ordered):
            if all(q.mde_paise <= effect for q in ordered[i:]):
                return point.n
        return 0

    def required_n(self, effect_paise: float | None = None) -> int:
        """The recommended batch size: the more conservative of the two rules.

        Taking the larger is deliberate. The two disagree because the fit
        under-describes the tail, and when a power calculation is uncertain the
        only safe direction to be wrong in is "too many observations".
        """
        return max(self.required_n_measured(effect_paise), self.required_n_fitted(effect_paise))

    def render(self) -> str:
        w = 70
        lines = [
            f"POWER ANALYSIS . two-sided alpha={self.alpha:.2f} . {self.power:.0%} power",
            "-" * w,
            "  the effect is FIXED -- it is a property of the simulator's recovery",
            "  rates. n is the only free variable. Tuning the effect until the",
            "  experiment succeeds would measure the generator, not the product.",
            "",
            f"  true ITT effect per decline ..  Rs {self.true_effect_paise / 100:>10,.2f}",
            "",
            f"  {'n':>7} {'runs':>5}  {'SE boot':>11}  {'SE mc':>11}  {'MDE @80%':>11}  detect?",
        ]
        for p in self.points:
            lines.append(
                f"  {p.n:>7} {p.n_runs:>5}  Rs {p.se_paise / 100:>8,.2f}  "
                f"Rs {p.se_mc_paise / 100:>8,.2f}  Rs {p.mde_paise / 100:>8,.2f}  "
                f"{'YES' if p.powered else 'no'}"
            )
        agree = [p.bootstrap_agreement for p in self.points if p.se_mc_paise > 0]
        lines += [
            "",
            f"  SE(n) = {self.scale / 100:,.0f} / sqrt(n)   "
            f"(observed exponent {self.observed_exponent:+.3f}, theory -0.500)",
            f"  bootstrap SE / Monte Carlo SE .  {np.mean(agree):.2f}" if agree else "",
            "    an independent check that the interval is honest -- the bootstrap",
            "    SE and the across-run spread of tau_hat are measuring the same thing.",
            "-" * w,
        ]
        return "\n".join(lines)

    # ── persistence ──────────────────────────────────────────────────────────
    # The curve costs ~20 minutes of live OPA evaluation. Re-measuring it during
    # a five-minute demo is not an option, and re-deriving it from memory would
    # be fabrication, so the measurement is saved as an artifact and reloaded.
    def to_json(self) -> str:
        return json.dumps(
            {
                "alpha": self.alpha,
                "power": self.power,
                "points": [
                    {
                        "n": p.n,
                        "se_paise": p.se_paise,
                        "se_mc_paise": p.se_mc_paise,
                        "mde_paise": p.mde_paise,
                        "itt_paise": p.itt_paise,
                        "n_actuated": p.n_actuated,
                        "n_runs": p.n_runs,
                    }
                    for p in self.points
                ],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> PowerCurve:
        doc = json.loads(raw)
        return cls(
            points=[PowerPoint(**pt) for pt in doc["points"]],
            alpha=doc.get("alpha", ALPHA),
            power=doc.get("power", POWER),
        )


def _default_client() -> PolicyClient:
    return PolicyClient()


def measure_point(
    n: int,
    seeds: Sequence[int],
    client_factory: Callable[[], PolicyClient] = _default_client,
    holdout_pct: int = DEFAULT_HOLDOUT_PCT,
    experiment_id: str = "power",
) -> PowerPoint:
    """Run the real pipeline at this n and read the estimator's own SE.

    The full pipeline, not a shortcut: policy refuses a large share of declines,
    so the ITT effect is much smaller than the simulator's raw treatment effect.
    Powering against the raw effect would prescribe a batch size that cannot
    detect what this experiment actually estimates.
    """
    ses: list[float] = []
    taus: list[float] = []
    itts: list[float] = []
    actuated: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        for i, seed in enumerate(seeds):
            result = run_batch(
                n=n,
                seed=seed,
                ledger_path=Path(tmp) / f"power_{n}_{seed}.db",
                client=client_factory(),
                # A distinct id per run re-randomises the arms, so the SE is not
                # conditioned on one lucky split.
                experiment_id=f"{experiment_id}-{n}-{i}",
                holdout_pct=holdout_pct,
            )
            if result.estimate is None:
                continue
            ses.append(result.estimate.se)
            taus.append(result.estimate.tau_hat)
            itts.append(result.true_itt_paise)
            actuated.append(result.n_actuated)

    se = float(np.median(ses)) if ses else 0.0
    return PowerPoint(
        n=n,
        se_paise=se,
        se_mc_paise=float(np.std(taus, ddof=1)) if len(taus) > 1 else 0.0,
        mde_paise=mde(se),
        itt_paise=float(np.mean(itts)) if itts else 0.0,
        n_actuated=int(np.mean(actuated)) if actuated else 0,
        n_runs=len(ses),
    )


def power_curve(
    grid: Sequence[int] = DEFAULT_GRID,
    seeds_per_point: int | None = None,
    seed0: int = 4100,
    client_factory: Callable[[], PolicyClient] = _default_client,
    holdout_pct: int = DEFAULT_HOLDOUT_PCT,
) -> PowerCurve:
    """Measure SE across a grid of batch sizes and let the curve pick n.

    `seeds_per_point=None` allocates replicates by observation budget, which is
    what keeps the small-n points from being read off noise.
    """
    curve = PowerCurve()
    for j, n in enumerate(grid):
        k = seeds_per_point if seeds_per_point else runs_for(n)
        seeds = [seed0 + j * 100 + i for i in range(k)]
        curve.points.append(
            measure_point(n, seeds, client_factory=client_factory, holdout_pct=holdout_pct)
        )
    return curve


__all__ = [
    "ALPHA",
    "DEFAULT_GRID",
    "POWER",
    "PowerCurve",
    "PowerPoint",
    "mde",
    "measure_point",
    "power_curve",
]
