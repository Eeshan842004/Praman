"""Escalation ladder T0-T4.

    T0  terminate        no action; notify the merchant
    T1  silent_retry     retry at a model-chosen time
    T2  rail_switch      card -> UPI, or an alternate instrument
    T3  customer_nudge   ask the customer to fix something
    T4  human_escalate   merchant ops queue

T1/T2/T3 are actions and require authorisation. T0 and T4 are not money actions
-- doing nothing needs no permission, and neither does asking a person -- so the
ladder treats them as terminal fallbacks rather than candidates to authorise.

The one rule that must not be optimised away: EVERY tier is evaluated and the
full deny-set of each is recorded. A `for tier in tiers: if allowed: return`
loop is faster and destroys the only artifact that makes the regulatory-deadlock
case legible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from praman.kernel.opa_client import PolicyClient, PolicyDecision
from praman.ledger.canonical import prob_str
from praman.taxonomy import load_taxonomy

TIER_ACTION: dict[str, str] = {
    "T0": "terminate",
    "T1": "silent_retry",
    "T2": "rail_switch",
    "T3": "customer_nudge",
    "T4": "human_escalate",
}

# Tiers that constitute an action and therefore need policy authorisation.
ACTIONABLE_TIERS: tuple[str, ...] = ("T1", "T2", "T3")
# Queried as well, because the audit trail shows whether escalation was legal.
QUERIED_TIERS: tuple[str, ...] = ("T1", "T2", "T3", "T4")


@dataclass(frozen=True, slots=True)
class DeclineContext:
    """Everything the policy needs to judge one decline.

    Counters are computed in Python and passed in as data (law: Rego has no
    database access, and windowing inside Rego makes tests non-deterministic).
    """

    cause: str
    max_posterior: float
    rail: str
    amount_paise: int
    network_category: int | None
    merchant_advice_code: str | None
    npci_retry_remark: str | None
    attempts_30d: int
    attempts_this_payment: int
    bin_attempts_1h: int
    customer_nudges_7d: int
    is_emandate: bool
    afa_completed: bool
    ms_since_pre_debit_notice: int
    ist_hour: int
    has_alternate_instrument: bool


@dataclass(frozen=True, slots=True)
class TierEvaluation:
    tier: str
    allow: bool
    deny_reasons: list[str]
    failed_closed: bool
    bundle_revision: str
    decision_id: str | None


@dataclass(frozen=True, slots=True)
class LadderOutcome:
    selected_tier: str
    is_action: bool
    reason: str
    proposed_tiers: tuple[str, ...]
    evaluations: dict[str, TierEvaluation] = field(default_factory=dict)
    # The exact objects OPA evaluated, kept so the ledger can persist one and
    # replay can re-run it against the pinned bundle.
    policy_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def bundle_revision(self) -> str:
        """The revision OPA reported. Identical across tiers in one pass, so the
        first non-unknown is authoritative."""
        for ev in self.evaluations.values():
            if ev.bundle_revision and ev.bundle_revision != "UNKNOWN":
                return ev.bundle_revision
        return "UNKNOWN"

    @property
    def decision_id(self) -> str | None:
        ev = self.evaluations.get(self.selected_tier)
        if ev and ev.decision_id:
            return ev.decision_id
        for e in self.evaluations.values():
            if e.decision_id:
                return e.decision_id
        return None

    # ── What goes on the DECISION row ────────────────────────────────────────
    # These three MUST describe the same tier. The ledger stores one input, one
    # verdict and one deny-set, and `praman verify` re-POSTs that input to the
    # pinned bundle and compares it against that verdict. If they described
    # different tiers, every replay would diverge and the attestation would be
    # measuring our own inconsistency rather than the policy.

    @property
    def recorded_tier(self) -> str:
        """The tier the DECISION row describes.

        Normally the selected one. T0 is never queried -- doing nothing needs no
        authorisation -- so a terminated decline records the first tier we DID
        ask about, which is what lets an auditor confirm the denial was correct.
        """
        if self.selected_tier in self.policy_inputs:
            return self.selected_tier
        return next(iter(self.policy_inputs), self.selected_tier)

    @property
    def recorded_policy_input(self) -> dict[str, Any]:
        return self.policy_inputs.get(self.recorded_tier, {})

    @property
    def recorded_deny_reasons(self) -> list[str]:
        ev = self.evaluations.get(self.recorded_tier)
        return list(ev.deny_reasons) if ev else []

    @property
    def recorded_opa_allow(self) -> bool:
        """OPA's verdict for `recorded_tier` -- NOT whether we acted.

        These differ on purpose. T4 is authorised but is not a money action, so
        it allows without actuating; `is_action` answers "did we do something",
        this answers "what did the policy say". Conflating them made the stored
        verdict unreplayable for every T0 and T4 row.
        """
        ev = self.evaluations.get(self.recorded_tier)
        return bool(ev.allow) if ev else False

    def as_tier_evaluations(self) -> dict[str, list[str]]:
        """Shape the ledger stores: tier -> deny reasons."""
        return {t: list(e.deny_reasons) for t, e in self.evaluations.items()}


def build_policy_input(ctx: DeclineContext, tier: str) -> dict[str, Any]:
    """Assemble the exact object OPA evaluates. This is persisted verbatim so
    replay can re-run it against the pinned bundle."""
    meta = load_taxonomy().cause_meta(ctx.cause)
    return {
        "cause_class": meta.cause_class,
        "tier": tier,
        "network_category": ctx.network_category,
        "merchant_advice_code": ctx.merchant_advice_code,
        "npci_retry_remark": ctx.npci_retry_remark,
        "attempts_30d": int(ctx.attempts_30d),
        "attempts_this_payment": int(ctx.attempts_this_payment),
        "bin_attempts_1h": int(ctx.bin_attempts_1h),
        "customer_nudges_7d": int(ctx.customer_nudges_7d),
        "is_emandate": bool(ctx.is_emandate),
        "amount_paise": int(ctx.amount_paise),
        "afa_completed": bool(ctx.afa_completed),
        "ms_since_pre_debit_notice": int(ctx.ms_since_pre_debit_notice),
        "rail": ctx.rail,
        "ist_hour": int(ctx.ist_hour),
        # 6-dp string: the rego does to_number() on it and the ledger hashes it.
        "max_posterior": prob_str(ctx.max_posterior),
        "has_alternate_instrument": bool(ctx.has_alternate_instrument),
    }


def proposed_tiers_for(cause: str) -> tuple[str, ...]:
    """Tiers the ladder will even ASK about for this cause, in preference order.

    Ladder eligibility and policy permission are different things, and
    conflating them misreads the audit trail. Policy may perfectly well allow
    T1 for a decline the ladder never proposed -- an authentication failure is
    not retryable, so a silent retry is pointless rather than illegal, and the
    ladder skips it. A reader seeing "T1 ALLOW" beside a T3 outcome would
    otherwise conclude we ignored a permitted action.

    Non-retryable causes never propose T1/T2. An expired card cannot be retried,
    but a nudge is both legal and the correct fix, so it goes straight to T3.
    A hard decline proposes nothing at all.

    Public because the dashboard renders this distinction and must derive it
    from the same function the kernel uses, not a copy of the rule.
    """
    meta = load_taxonomy().cause_meta(cause)
    if meta.cause_class == "hard":
        return ()

    order = [meta.default_tier] + [t for t in ACTIONABLE_TIERS if t != meta.default_tier]
    if not meta.retryable:
        order = [t for t in order if t not in ("T1", "T2")]
    return tuple(t for t in order if t in ACTIONABLE_TIERS)


def _candidates(ctx: DeclineContext) -> tuple[str, ...]:
    return proposed_tiers_for(ctx.cause)


def evaluate_ladder(ctx: DeclineContext, client: PolicyClient) -> LadderOutcome:
    """Evaluate every tier, then select. Never the other way round."""
    evaluations: dict[str, TierEvaluation] = {}
    policy_inputs: dict[str, dict[str, Any]] = {}
    for tier in QUERIED_TIERS:
        policy_inputs[tier] = build_policy_input(ctx, tier)
        d: PolicyDecision = client.evaluate(policy_inputs[tier])
        evaluations[tier] = TierEvaluation(
            tier=tier,
            allow=d.allow,
            deny_reasons=list(d.deny_reasons),
            failed_closed=d.failed_closed,
            bundle_revision=d.bundle_revision,
            decision_id=d.decision_id,
        )

    meta = load_taxonomy().cause_meta(ctx.cause)
    proposed = _candidates(ctx)

    # Every exit carries the evidence, by construction. When each `return` built
    # its own LadderOutcome, the allow path omitted `policy_inputs` -- so the
    # ONLY decisions that ever reached actuation were the ones that stored an
    # empty policy input, and replay attestation had nothing to replay on
    # precisely the rows that authorised money movement. A single constructor
    # makes that omission unrepresentable rather than merely fixed.
    def outcome(tier: str, is_action: bool, reason: str) -> LadderOutcome:
        return LadderOutcome(
            selected_tier=tier,
            is_action=is_action,
            reason=reason,
            proposed_tiers=proposed,
            evaluations=evaluations,
            policy_inputs=policy_inputs,
        )

    if meta.cause_class == "hard":
        return outcome("T0", False, f"{ctx.cause} is a hard decline; no action is legal")

    for tier in proposed:
        if evaluations[tier].allow:
            return outcome(tier, True, f"policy allowed {TIER_ACTION[tier]}")

    if evaluations["T4"].allow:
        return outcome("T4", False, "every permitted action was denied; escalating to a human")

    # Nothing is legal, including escalation -- which is what an unreachable
    # policy engine looks like. Terminate; never act on an unauthorised path.
    return outcome("T0", False, "no tier authorised; terminating")


__all__ = [
    "ACTIONABLE_TIERS",
    "QUERIED_TIERS",
    "TIER_ACTION",
    "DeclineContext",
    "LadderOutcome",
    "TierEvaluation",
    "build_policy_input",
    "evaluate_ladder",
    "proposed_tiers_for",
]
