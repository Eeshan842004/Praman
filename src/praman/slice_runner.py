"""End-to-end batch runner: the vertical slice.

decline -> normalise -> posterior -> cluster arm -> ladder -> OPA -> ledger
DECISION -> simulated actuation -> ledger OUTCOME -> estimate over the ledger.

Attribution here is the TAXONOMY POSTERIOR, not a trained model. That is
deliberate: it makes the model an upgrade to a working system rather than a
dependency of one. Swapping LightGBM in later changes `attribution_source` from
"heuristic" to "ml" and nothing else.

Two invariants this file exists to hold:

  Law #4  the DECISION row is written BEFORE any actuation, always.
  Law #7  compliance counters advance on ACTUATION, never on decisions. A
          decision policy refused is not an attempt.

The estimand is INTENTION TO TREAT. A treatment-arm payment that policy declined
to act on still counts as treated, because what is being measured is the system
as deployed -- refusals included. Per-protocol would quietly credit the agent
for the payments it chose not to touch.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from praman.kernel.ladder import TIER_ACTION, DeclineContext, evaluate_ladder
from praman.kernel.opa_client import PolicyClient
from praman.ledger.chain import append, connect
from praman.ledger.records import ActuationRecord, DecisionRecord, OutcomeRecord
from praman.measure.assign import assign_arm
from praman.measure.from_ledger import estimate_from_ledger, naive_gross_from_ledger
from praman.measure.harness import Estimate
from praman.metrics import ATTRIBUTION_CONFIDENCE, DECISIONS, POLICY_VIOLATIONS
from praman.sim.generator import DeclineBatch, SyntheticDecline, generate_batch
from praman.taxonomy import Observation, load_taxonomy

ATTRIBUTION_VERSION = "taxonomy-v1"
RETRY_DELAY_MS = 6 * 60 * 60 * 1000  # placeholder for the Phase 6 hazard model
OUTCOME_DELAY_MS = 24 * 60 * 60 * 1000


@dataclass(slots=True)
class RunResult:
    experiment_id: str
    ledger_path: Path
    n_declines: int = 0
    n_treatment: int = 0
    n_holdout: int = 0
    n_actuated: int = 0
    policy_violations: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    true_itt_paise: float = 0.0
    naive_gross_paise: float = 0.0
    estimate: Estimate | None = None

    def render(self) -> str:
        w = 66
        lines = [
            f"BATCH {self.experiment_id} . {self.n_declines} declines "
            f"({self.n_treatment} treatment / {self.n_holdout} holdout)",
            "-" * w,
            "Ladder:",
        ]
        for tier in ("T0", "T1", "T2", "T3", "T4"):
            n = self.tier_counts.get(tier, 0)
            if n:
                lines.append(f"  {tier} {TIER_ACTION[tier]:<16} {n:>5}")
        lines += [
            f"  actuated (law #7 counter) {self.n_actuated:>5}",
            f"  policy violations         {self.policy_violations:>5}",
            "-" * w,
        ]
        if self.estimate is not None:
            e = self.estimate
            lines += [
                "Incremental recovery per decline (intention to treat):",
                f"  estimate ...............  Rs {e.tau_hat / 100:>12,.2f}",
                f"  95% CI .................. [Rs {e.ci_lo / 100:,.2f}, Rs {e.ci_hi / 100:,.2f}]",
                f"  CUPED variance reduction  {e.variance_reduction:>12.0%}",
                f"  clusters (customers) ...  {e.n_clusters_treatment} / {e.n_clusters_holdout}",
                "",
                f"  SEALED TRUTH for this batch  Rs {self.true_itt_paise / 100:>10,.2f}",
                f"  covered by the interval ...  "
                f"{'YES' if e.ci_lo <= self.true_itt_paise <= e.ci_hi else 'NO'}",
                "",
                "Industry-standard gross recovery (no holdout):",
                f"  reported ...............  Rs {self.naive_gross_paise / 100:>12,.2f}"
                "   <- no counterfactual",
                "-" * w,
            ]
        return "\n".join(lines)


def _append(conn: sqlite3.Connection, row: dict) -> int:
    """Append and return the assigned sequence number."""
    h = append(conn, row)
    return int(conn.execute("SELECT seq FROM ledger WHERE entry_hash = ?", (h,)).fetchone()[0])


def _observation(d: SyntheticDecline) -> Observation:
    return Observation(
        rail=d.rail,
        symbol=d.symbol,
        raw_code=d.symbol,
        network_category=d.network_category,
        merchant_advice_code=d.merchant_advice_code,
        npci_retry_remark=d.npci_retry_remark,
        cvv_result=d.cvv_result,
        expiry_valid=d.expiry_valid,
    )


def run_batch(
    n: int = 1000,
    seed: int = 42,
    ledger_path: str | Path = "data/ledger.db",
    client: PolicyClient | None = None,
    experiment_id: str = "praman-v1",
    holdout_pct: int = 10,
    region: str = "IN",
    batch: DeclineBatch | None = None,
) -> RunResult:
    tax = load_taxonomy()
    batch = batch or generate_batch(n=n, seed=seed, region=region)
    client = client or PolicyClient()
    conn = connect(ledger_path)

    result = RunResult(experiment_id=experiment_id, ledger_path=Path(ledger_path))
    tiers: Counter[str] = Counter()

    # Law #7: these advance ONLY when an actuation executes.
    attempts_30d: dict[str, int] = defaultdict(int)
    attempts_payment: dict[str, int] = defaultdict(int)
    bin_attempts: dict[str, int] = defaultdict(int)
    nudges_7d: dict[str, int] = defaultdict(int)

    actioned: dict[str, bool] = {}

    try:
        for d in batch.declines:
            obs = _observation(d)
            posterior = tax.posterior(obs, region=region)
            cause = max(posterior, key=lambda c: posterior[c])
            confidence = posterior[cause]
            ATTRIBUTION_CONFIDENCE.observe(confidence)

            ctx = DeclineContext(
                cause=cause,
                max_posterior=confidence,
                rail=d.rail,
                amount_paise=d.amount_paise,
                network_category=d.network_category,
                merchant_advice_code=d.merchant_advice_code,
                npci_retry_remark=d.npci_retry_remark,
                attempts_30d=attempts_30d[d.customer_id],
                attempts_this_payment=attempts_payment[d.payment_id],
                bin_attempts_1h=bin_attempts[d.bin],
                customer_nudges_7d=nudges_7d[d.customer_id],
                is_emandate=d.is_emandate,
                afa_completed=d.afa_completed,
                ms_since_pre_debit_notice=d.ms_since_pre_debit_notice,
                ist_hour=d.ist_hour,
                has_alternate_instrument=d.has_alternate_instrument,
            )

            # The ladder runs for BOTH arms. We record what we would have done
            # for the holdout too -- otherwise there is no way to show a
            # reviewer the arms were comparable. Only the ACTION is withheld.
            ladder = evaluate_ladder(ctx, client)
            arm = assign_arm(experiment_id, d.customer_id, holdout_pct)
            tiers[ladder.selected_tier] += 1
            DECISIONS.labels(tier=ladder.selected_tier, allow=str(ladder.is_action).lower()).inc()

            # ---- Law #4: record BEFORE acting -------------------------------
            decision_seq = _append(
                conn,
                DecisionRecord(
                    ts_ms=d.ts_ms,
                    experiment_id=experiment_id,
                    holdout_pct=holdout_pct,
                    payment_id=d.payment_id,
                    customer_id=d.customer_id,
                    arm=arm,
                    attempt_no=attempts_payment[d.payment_id] + 1,
                    rail=d.rail,
                    symbol=d.symbol,
                    region=region,
                    cause=cause,
                    posterior=posterior,
                    attribution_source="heuristic",
                    attribution_version=ATTRIBUTION_VERSION,
                    tier=ladder.selected_tier,
                    tier_evaluations=ladder.as_tier_evaluations(),
                    opa_allow=ladder.is_action,
                    deny_reasons=ladder.selected_deny_reasons,
                    policy_input=ladder.selected_policy_input,
                    bundle_revision=ladder.bundle_revision,
                    decision_id=ladder.decision_id,
                    amount_paise=d.amount_paise,
                    cuped_covariate=d.prior_success_rate,
                    covariate_asof_ms=d.covariate_asof_ms,
                    scheduled_for_ms=d.ts_ms + RETRY_DELAY_MS if ladder.is_action else None,
                    payload={"symbol": d.symbol, "rail": d.rail, "redacted": True},
                ).to_row(),
            )

            # ---- Actuate: treatment arm only, and only if policy allowed ----
            take_action = ladder.is_action and arm == "treatment"
            actioned[d.payment_id] = take_action

            if take_action:
                if not ladder.evaluations[ladder.selected_tier].allow:
                    # Unreachable by construction; counted so the gauge is a
                    # measurement rather than an assertion.
                    result.policy_violations += 1
                    POLICY_VIOLATIONS.inc()

                _append(
                    conn,
                    ActuationRecord(
                        ts_ms=d.ts_ms + RETRY_DELAY_MS,
                        experiment_id=experiment_id,
                        holdout_pct=holdout_pct,
                        payment_id=d.payment_id,
                        customer_id=d.customer_id,
                        arm=arm,
                        decision_seq=decision_seq,
                        attempt_no=attempts_payment[d.payment_id] + 1,
                        rail=d.rail,
                        tier=ladder.selected_tier,
                        executed=True,
                        actuation_result="success" if d.y1_recovered else "failure",
                    ).to_row(),
                )
                result.n_actuated += 1
                attempts_30d[d.customer_id] += 1
                attempts_payment[d.payment_id] += 1
                bin_attempts[d.bin] += 1
                if ladder.selected_tier == "T3":
                    nudges_7d[d.customer_id] += 1

            # ---- Outcome ----------------------------------------------------
            recovered = d.y1_recovered if take_action else d.y0_recovered
            _append(
                conn,
                OutcomeRecord(
                    ts_ms=d.ts_ms + OUTCOME_DELAY_MS,
                    experiment_id=experiment_id,
                    holdout_pct=holdout_pct,
                    payment_id=d.payment_id,
                    customer_id=d.customer_id,
                    arm=arm,
                    decision_seq=decision_seq,
                    recovered=recovered,
                    recovered_at_ms=d.ts_ms + OUTCOME_DELAY_MS if recovered else None,
                    recovered_amount_paise=d.amount_paise if recovered else 0,
                    outcome_source=("actuated" if take_action else "natural")
                    if recovered
                    else "none",
                ).to_row(),
            )

            result.n_declines += 1
            result.n_treatment += arm == "treatment"
            result.n_holdout += arm == "holdout"

        result.tier_counts = dict(tiers)
        result.true_itt_paise = batch.sealed_truth(actioned)
        result.estimate = estimate_from_ledger(conn, experiment_id)
        result.naive_gross_paise = naive_gross_from_ledger(conn, experiment_id)
    finally:
        conn.close()
        client.close()

    return result


__all__ = ["ATTRIBUTION_VERSION", "RunResult", "run_batch"]
