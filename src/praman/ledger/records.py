"""Ledger record types.

The ledger is append-only, so an outcome CANNOT be an update to a decision row.
Three entry types share one hash chain, discriminated by `entry_type`:

    DECISION   what we inferred, and what policy authorised
    ACTUATION  what we actually did      <- compliance counters read ONLY these
    OUTCOME    what happened, including natural recovery in the holdout arm

Law #7 lives here: `attempts_30d` counts ACTUATION rows with executed=1, never
webhook deliveries and never decisions. A decision that policy denied is not an
attempt, and a webhook redelivery is an observation, not an attempt.

Every field is hashed. A column outside the hash is not evidence, so there are
none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from praman.ledger.canonical import prob_str

# Bumping this is a MIGRATION, never an edit: it changes the canonical bytes and
# therefore invalidates every ledger written under the previous version.
LEDGER_SCHEMA_VERSION = 2


class EntryType(StrEnum):
    DECISION = "DECISION"
    ACTUATION = "ACTUATION"
    OUTCOME = "OUTCOME"


def _json(obj: Any) -> str:
    """Canonical JSON for a nested column. Sorted and compact, so the enclosing
    row's bytes are stable regardless of dict insertion order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def prob_vector_json(vector: dict[str, float]) -> str:
    """A probability distribution as a column: 6-dp strings, key-sorted.

    Storing only the argmax would destroy the ambiguity that is the product --
    the whole claim is that code 05 does not resolve to one cause, and an
    auditor has to be able to see the spread we acted on.
    """
    return _json({k: prob_str(v) for k, v in vector.items()})


@dataclass(frozen=True, slots=True)
class _Envelope:
    """Fields present on every entry type."""

    ts_ms: int
    experiment_id: str
    holdout_pct: int
    payment_id: str
    customer_id: str
    arm: str

    def _base(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ts_ms": int(self.ts_ms),
            "experiment_id": self.experiment_id,
            # arm = f(experiment_id, customer_id, holdout_pct). All three must be
            # recorded or the assignment cannot be re-derived by an auditor.
            "holdout_pct": int(self.holdout_pct),
            "payment_id": self.payment_id,
            "customer_id": self.customer_id,
            "arm": self.arm,
        }


def _blank() -> dict[str, Any]:
    """Every hashed column, defaulted. Columns not applicable to an entry type
    are explicitly None rather than absent, so all three types serialise over
    an identical key set."""
    return {
        "entry_type": None,
        "attempt_no": None,
        "rail": None,
        "symbol": None,
        "region": None,
        "cause": None,
        "posterior": None,
        "posterior_vector": None,
        "attribution_source": None,
        "attribution_version": None,
        "tier": None,
        "tier_evaluations": None,
        "opa_allow": None,
        "deny_reasons": None,
        "policy_input_json": None,
        "bundle_revision": None,
        "decision_id": None,
        "amount_paise": None,
        "cuped_covariate": None,
        "covariate_asof_ms": None,
        "scheduled_for_ms": None,
        "decision_seq": None,
        "executed": None,
        "actuation_result": None,
        "recovered": None,
        "recovered_at_ms": None,
        "recovered_amount_paise": None,
        "outcome_source": None,
        "payload_json": None,
    }


@dataclass(frozen=True, slots=True)
class DecisionRecord(_Envelope):
    """What we inferred and what policy authorised. Written BEFORE actuation."""

    attempt_no: int
    rail: str
    symbol: str
    region: str
    cause: str
    posterior: dict[str, float]
    attribution_source: str  # "heuristic" | "ml"
    attribution_version: str
    tier: str
    tier_evaluations: dict[str, list[str]]
    opa_allow: bool
    deny_reasons: list[str]
    policy_input: dict[str, Any]
    bundle_revision: str
    decision_id: str | None
    amount_paise: int
    cuped_covariate: float
    covariate_asof_ms: int
    scheduled_for_ms: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # CUPED is unbiased only if the covariate is strictly pre-treatment.
        # We do not assert that in prose -- we timestamp it and check.
        if self.covariate_asof_ms > self.ts_ms:
            raise ValueError(
                "cuped_covariate must be pre-treatment: "
                f"covariate_asof_ms={self.covariate_asof_ms} > ts_ms={self.ts_ms}"
            )

    def to_row(self) -> dict[str, Any]:
        row = _blank() | self._base()
        row.update(
            entry_type=str(EntryType.DECISION),
            attempt_no=int(self.attempt_no),
            rail=self.rail,
            symbol=self.symbol,
            region=self.region,
            cause=self.cause,
            posterior=prob_str(max(self.posterior.values())),
            posterior_vector=prob_vector_json(self.posterior),
            attribution_source=self.attribution_source,
            attribution_version=self.attribution_version,
            tier=self.tier,
            # Every tier, not just the chosen one. The regulatory-deadlock case
            # is only legible if the full conflict matrix survives.
            tier_evaluations=_json({k: sorted(v) for k, v in self.tier_evaluations.items()}),
            opa_allow=int(bool(self.opa_allow)),
            deny_reasons=_json(sorted(self.deny_reasons)),
            # Replay re-evaluates this against the pinned bundle. Without it,
            # attestation is only a hash check -- half the claim.
            policy_input_json=_json(self.policy_input),
            bundle_revision=self.bundle_revision,
            decision_id=self.decision_id,
            amount_paise=int(self.amount_paise),
            cuped_covariate=prob_str(self.cuped_covariate),
            covariate_asof_ms=int(self.covariate_asof_ms),
            scheduled_for_ms=None if self.scheduled_for_ms is None else int(self.scheduled_for_ms),
            payload_json=_json(self.payload),
        )
        return row


@dataclass(frozen=True, slots=True)
class ActuationRecord(_Envelope):
    """What we actually did. Compliance counters read ONLY these rows."""

    decision_seq: int
    attempt_no: int
    rail: str
    tier: str
    executed: bool
    actuation_result: str  # "success" | "failure" | "skipped"

    def to_row(self) -> dict[str, Any]:
        row = _blank() | self._base()
        row.update(
            entry_type=str(EntryType.ACTUATION),
            decision_seq=int(self.decision_seq),
            attempt_no=int(self.attempt_no),
            rail=self.rail,
            tier=self.tier,
            executed=int(bool(self.executed)),
            actuation_result=self.actuation_result,
        )
        return row


@dataclass(frozen=True, slots=True)
class OutcomeRecord(_Envelope):
    """What happened.

    `outcome_source` separates recovery we CAUSED from recovery that would have
    happened anyway. Holdout outcomes are the counterfactual baseline; without
    them recorded there is nothing to subtract and the estimate is gross.
    """

    decision_seq: int
    recovered: bool
    recovered_amount_paise: int
    outcome_source: str  # "actuated" | "natural" | "none"
    recovered_at_ms: int | None = None

    def to_row(self) -> dict[str, Any]:
        row = _blank() | self._base()
        row.update(
            entry_type=str(EntryType.OUTCOME),
            decision_seq=int(self.decision_seq),
            recovered=int(bool(self.recovered)),
            recovered_at_ms=None if self.recovered_at_ms is None else int(self.recovered_at_ms),
            recovered_amount_paise=int(self.recovered_amount_paise),
            outcome_source=self.outcome_source,
        )
        return row


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "ActuationRecord",
    "DecisionRecord",
    "EntryType",
    "OutcomeRecord",
    "prob_vector_json",
]
