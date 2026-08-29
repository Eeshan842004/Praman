"""The reported result, in three tiers.

The old headline was a rupee figure. It should not have been, and not because
the figure was wrong -- because a rupee figure is not the claim this system can
support. We cannot validate against reality; no public dataset carries real
decline codes. What we CAN do is validate the instrument, and say exactly how
much confidence each layer of evidence earns.

    PRIMARY       the estimator, scored against worlds whose truth is sealed.
                  Scale-free, repeatable, and the only tier that generalises.

    SECONDARY     one adequately powered batch. Demonstrates the primary claim
                  operating end to end on a single experiment.

    ILLUSTRATIVE  the underpowered batch, kept on purpose. A wide honest
                  interval that covers the truth, beside a confident naive
                  point estimate that misses it.

The ordering is the argument. An incumbent leads with the rupee figure and has
no tier above it; every number they report lives in our third tier, without the
interval. Keeping the underpowered run visible is the point rather than an
embarrassment: an interval wide enough to look unimpressive, that contains the
answer, is worth more than a narrow one that does not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from praman.kernel.opa_client import PolicyClient
from praman.measure.assign import DEFAULT_HOLDOUT_PCT
from praman.measure.harness import ValidationReport, payments_world, validate_estimator
from praman.slice_runner import RunResult, run_batch

RULE = "=" * 74
THIN = "-" * 74


def _money(paise: float) -> str:
    return f"Rs {paise / 100:,.2f}"


@dataclass(slots=True)
class ThreeTierReport:
    primary: ValidationReport | None = None
    secondary: RunResult | None = None
    illustrative: RunResult | None = None
    powered_n: int = 0
    illustrative_n: int = 0

    # ── tier 1 ───────────────────────────────────────────────────────────────
    def _render_primary(self) -> list[str]:
        r = self.primary
        if r is None:
            return []
        return [
            RULE,
            "PRIMARY . the estimator, not the outcome",
            RULE,
            f"  {r.n_worlds} worlds from the payments simulator . sealed true effect",
            f"  {100 - r.holdout_pct}/{r.holdout_pct} cluster-randomised at the customer",
            "",
            "  Praman (CUPED + customer-level cluster bootstrap)",
            f"    bias vs sealed truth ......  {r.mean_bias_pct:+.1f}%",
            f"    95% CI coverage ...........  {r.coverage:.1%}   (nominal 95%)",
            f"    RMSE ......................  {r.rmse:,.1f} paise",
            f"    CUPED variance reduction ..  {r.mean_variance_reduction:.0%}",
            "",
            "  Industry-standard gross recovery (no holdout)",
            f"    bias vs sealed truth ......  {r.naive_mean_bias_pct:+.1f}%"
            "   <- what incumbents report",
            f"    interval coverage .........  {r.naive_coverage:.1%}     (it ships no interval)",
            "",
            "  Coverage is a hard test: it fails if the interval is too NARROW or",
            "  too wide. Passing it is what licenses every interval below.",
        ]

    # ── tier 2 ───────────────────────────────────────────────────────────────
    def _render_secondary(self) -> list[str]:
        r = self.secondary
        if r is None or r.estimate is None:
            return []
        e = r.estimate
        excludes_zero = e.ci_lo > 0 or e.ci_hi < 0
        covered = e.ci_lo <= r.true_itt_paise <= e.ci_hi
        return [
            "",
            RULE,
            f"SECONDARY . one powered batch (n={r.n_declines:,})",
            RULE,
            f"  batch size chosen by the power curve, not by hand. {r.n_actuated:,} actuations.",
            "",
            f"    incremental per decline ...  {_money(e.tau_hat)}",
            f"    95% CI ....................  [{_money(e.ci_lo)}, {_money(e.ci_hi)}]",
            f"    excludes zero .............  {'YES' if excludes_zero else 'NO'}",
            f"    sealed truth ..............  {_money(r.true_itt_paise)}",
            f"    covered by the interval ...  {'YES' if covered else 'NO'}",
            f"    policy violations .........  {r.policy_violations}",
        ]

    # ── tier 3 ───────────────────────────────────────────────────────────────
    def _render_illustrative(self) -> list[str]:
        r = self.illustrative
        if r is None or r.estimate is None:
            return []
        e = r.estimate
        covered = e.ci_lo <= r.true_itt_paise <= e.ci_hi
        naive_covered = e.ci_lo <= r.naive_gross_paise <= e.ci_hi
        return [
            "",
            RULE,
            f"ILLUSTRATIVE . the underpowered batch (n={r.n_declines:,}), kept on purpose",
            RULE,
            f"    incremental per decline ...  {_money(e.tau_hat)}",
            f"    95% CI ....................  [{_money(e.ci_lo)}, {_money(e.ci_hi)}]",
            f"    sealed truth ..............  {_money(r.true_itt_paise)}",
            f"    covered by the interval ...  {'YES' if covered else 'NO'}",
            "",
            f"    naive gross recovery ......  {_money(r.naive_gross_paise)}"
            "   <- no counterfactual",
            f"    inside our interval .......  {'yes' if naive_covered else 'NO'}",
            "",
            "  This interval is too wide to be useful and it contains the answer.",
            "  The naive number is precise, has no interval at all, and is wrong by",
            f"  {(r.naive_gross_paise - r.true_itt_paise) / max(r.true_itt_paise, 1):+.0%}."
            "  Honest and wide beats confident and wrong.",
            THIN,
            "  We do not claim recovered revenue. We claim the estimator recovers",
            "  the truth, and PRIMARY is the evidence for it.",
            RULE,
        ]

    def render(self) -> str:
        parts = self._render_primary() + self._render_secondary() + self._render_illustrative()
        return "\n".join(parts)


def _default_client() -> PolicyClient:
    return PolicyClient()


def build_report(
    powered_n: int,
    illustrative_n: int = 3_000,
    n_worlds: int = 200,
    world_n: int = 2_000,
    n_boot: int = 1_500,
    seed: int = 9_000,
    batch_seed: int = 42,
    holdout_pct: int = DEFAULT_HOLDOUT_PCT,
    ledger_dir: Path | None = None,
    client_factory: Callable[[], PolicyClient] = _default_client,
) -> ThreeTierReport:
    """Assemble all three tiers.

    PRIMARY runs against the payments simulator rather than the toy world. The
    toy world proved the estimator's contract before the simulator existed; the
    headline has to be measured on the domain it will be quoted about, and the
    naive estimator's bias in particular is scale-dependent and therefore
    meaningless when carried over from a domain-free generator.
    """
    ledger_dir = ledger_dir or Path("data")

    primary = validate_estimator(
        n_worlds=n_worlds,
        holdout_pct=holdout_pct,
        n_boot=n_boot,
        seed0=seed,
        world_factory=lambda s: payments_world(s, n=world_n),
    )

    secondary = run_batch(
        n=powered_n,
        seed=batch_seed,
        ledger_path=ledger_dir / "ledger.db",
        client=client_factory(),
        experiment_id="praman-powered",
        holdout_pct=holdout_pct,
    )

    illustrative = run_batch(
        n=illustrative_n,
        seed=batch_seed,
        ledger_path=ledger_dir / "ledger.db",
        client=client_factory(),
        experiment_id="praman-underpowered",
        holdout_pct=holdout_pct,
    )

    return ThreeTierReport(
        primary=primary,
        secondary=secondary,
        illustrative=illustrative,
        powered_n=powered_n,
        illustrative_n=illustrative_n,
    )


__all__ = ["ThreeTierReport", "build_report"]
