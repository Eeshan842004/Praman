"""Deterministic cluster randomisation (C5, mitigates S7).

Two properties matter and both come from the same choice.

Cluster at the CUSTOMER, not the payment. Subscription declines repeat per
customer, so payment-level randomisation violates SUTVA: a nudge sent for
customer A's payment #1 causes a wallet top-up that recovers customer A's
payment #2 -- which may sit in the holdout. Treatment leaks into control and
biases the estimate toward zero.

Assign by hash, not by RNG. `random` carries seed state, so an arm could not be
re-derived from the ledger and the experiment would not be auditable. A hash of
(experiment_id, customer_id) is pure: identical on every machine, re-derivable
by any auditor from the customer id alone, and impossible to manipulate after
the fact.
"""

from __future__ import annotations

import hashlib

BUCKETS = 10_000


def bucket(experiment_id: str, unit_id: str) -> int:
    """Map a unit into [0, 10000) uniformly and deterministically."""
    key = f"{experiment_id}|{unit_id}".encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") % BUCKETS


def assign_arm(experiment_id: str, customer_id: str, holdout_pct: int = 10) -> str:
    """Return 'holdout' or 'treatment' for a customer.

    The unit of randomisation is the customer, so every payment belonging to
    them lands in the same arm.
    """
    if not 0 <= holdout_pct <= 100:
        raise ValueError(f"holdout_pct must be in [0, 100], got {holdout_pct}")
    return "holdout" if bucket(experiment_id, customer_id) < holdout_pct * 100 else "treatment"


__all__ = ["BUCKETS", "assign_arm", "bucket"]
