"""Read the experiment back out of the ledger and estimate the effect.

The measurement reads the SAME append-only evidence file that `praman verify`
attests. There is no separate analytics store, which means the number in the
report and the number an auditor can re-derive are the same number.

Outcome unit is paise recovered, so the estimate is "incremental rupees per
decline" rather than a rate -- the quantity a merchant actually cares about.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from praman.measure.harness import Estimate, estimate_ate

# Join OUTCOME rows to the DECISION they came from. In an append-only model the
# outcome cannot update the decision, so provenance runs through decision_seq.
_QUERY = """
SELECT  o.arm,
        o.customer_id,
        o.recovered_amount_paise,
        d.cuped_covariate
FROM    ledger o
JOIN    ledger d ON d.seq = o.decision_seq AND d.entry_type = 'DECISION'
WHERE   o.entry_type = 'OUTCOME'
  AND   o.experiment_id = ?
ORDER BY o.seq
"""


def load_experiment(
    conn: sqlite3.Connection, experiment_id: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (y, treated, cluster_id, covariate) for the estimator.

    `cluster_id` is the CUSTOMER, not the payment -- the randomisation unit and
    the bootstrap unit have to be the same thing or the interval is wrong (S7).
    """
    rows = conn.execute(_QUERY, (experiment_id,)).fetchall()
    if not rows:
        raise ValueError(f"no outcomes recorded for experiment {experiment_id!r}")

    arms, customers, amounts, covariates = zip(*rows, strict=True)
    return (
        np.array(amounts, dtype=float),
        np.array([a == "treatment" for a in arms], dtype=int),
        np.array(customers),
        np.array([float(c) for c in covariates], dtype=float),
    )


def estimate_from_ledger(
    conn: sqlite3.Connection,
    experiment_id: str,
    *,
    n_boot: int = 2000,
    seed: int = 0,
) -> Estimate:
    y, treated, cluster_id, covariate = load_experiment(conn, experiment_id)
    return estimate_ate(y, treated, cluster_id, covariate, n_boot=n_boot, seed=seed)


def naive_gross_from_ledger(conn: sqlite3.Connection, experiment_id: str) -> float:
    """What the industry reports: mean recovered among the treated, no holdout.

    Kept beside the honest estimator so the two can be shown together. On its
    own it is not an effect estimate at all -- it has no counterfactual.
    """
    y, treated, _, _ = load_experiment(conn, experiment_id)
    mask = treated.astype(bool)
    return float(y[mask].mean()) if mask.any() else 0.0


__all__ = ["estimate_from_ledger", "load_experiment", "naive_gross_from_ledger"]
