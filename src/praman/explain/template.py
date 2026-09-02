"""The deterministic explanation. Always available, always correct.

This renders from the DECISION RECORD, which was written to the ledger before
anything was actuated. So the explanation describes a decision that has already
been made and attested -- it is a view over evidence, never an input to it.

It exists first, and unconditionally, for one reason: an external API must not
be able to break the demo. Everything the LLM layer does is an enhancement on
top of text that is already rendered and already true.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The merchant-facing meaning of each rule. A deny reason is a machine token;
# nobody outside this repo should have to read `npci_autopay_blackout_window`
# and infer what it means for their money.
REASON_TEXT: dict[str, str] = {
    "hard_decline": "the issuer refused permanently, so no retry is legal",
    "visa_cat1": "Visa classifies this decline as never-retryable",
    "mac_03": "the issuer sent merchant advice code 03 (do not try again)",
    "mac_21": "the issuer sent merchant advice code 21 (recurring stopped)",
    "npci_no_retry": "NPCI marked this decline do-not-reinitiate",
    "visa_network_cap": "the 30-day network retry cap is already reached",
    "per_payment_cap": "this payment has used all its permitted attempts",
    "bin_velocity": "too many attempts on this card range in the last hour",
    "nudge_fatigue_7d": "this customer has already been contacted the maximum number of times",
    "rbi_afa_required": "RBI requires additional factor authentication above this amount",
    "rbi_pre_debit_notice_not_elapsed": "RBI's 24-hour pre-debit notice has not elapsed",
    "npci_autopay_blackout_window": "NPCI blocks AutoPay execution between 10:00 and 13:00 IST",
    "low_confidence": "we are not confident enough about the cause to act automatically",
    "no_alternate_instrument": "this customer has no other saved payment method",
    "opa_unavailable": "the policy engine was unreachable, so we failed closed",
}

ACTION_TEXT: dict[str, str] = {
    "terminate": "stopped and told you",
    "silent_retry": "scheduled a silent retry",
    "rail_switch": "switched to another payment method",
    "customer_nudge": "asked the customer to fix it",
    "human_escalate": "sent it to your ops queue for a human",
}

CAUSE_TEXT: dict[str, str] = {
    "INSUFFICIENT_FUNDS": "insufficient funds",
    "ISSUER_RISK_BLOCK": "an issuer risk block",
    "VELOCITY_LIMIT": "a velocity limit",
    "AUTH_FAILURE": "an authentication failure",
    "TECHNICAL_DECLINE": "a technical fault at the bank or gateway",
    "EXPIRED_OR_INVALID_CREDENTIAL": "an expired or invalid card",
    "INSTRUMENT_DISABLED": "a disabled payment instrument",
    "LOST_STOLEN_FRAUD": "a card reported lost or stolen",
    "MANDATE_TERMINATED": "a cancelled mandate",
}


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """Exactly the fields of a ledger DECISION row that an explanation may read.

    Deliberately narrow. Anything not here cannot reach the prompt, so a future
    field cannot leak into an external API by accident.
    """

    payment_id: str
    cause: str
    confidence: float
    tier: str
    action: str
    amount_paise: int
    rail: str
    deny_reasons: list[str] = field(default_factory=list)
    tier_evaluations: dict[str, list[str]] = field(default_factory=dict)
    bundle_revision: str = ""
    ledger_seq: int | None = None


def _cause(name: str) -> str:
    return CAUSE_TEXT.get(name, name.replace("_", " ").lower())


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def render_template(s: DecisionSummary) -> str:
    """Plain, complete, and true regardless of what any model is doing."""
    confidence = (
        "confident" if s.confidence >= 0.7 else "fairly sure" if s.confidence >= 0.4 else "unsure"
    )
    lines = [
        f"{_rupees(s.amount_paise)} on {s.payment_id} failed on the "
        f"{s.rail.replace('_', ' ')} rail.",
        f"We are {confidence} the cause was {_cause(s.cause)} "
        f"(p={s.confidence:.2f}), and we {ACTION_TEXT.get(s.action, s.action)}.",
    ]

    blocked = {t: r for t, r in sorted(s.tier_evaluations.items()) if r}
    if blocked:
        lines.append("")
        lines.append("What we were not allowed to do, and why:")
        for tier, reasons in blocked.items():
            for reason in sorted(reasons):
                explanation = REASON_TEXT.get(reason, reason)
                lines.append(f"  {tier}  {reason} - {explanation}")
    else:
        lines.append("")
        lines.append("No rule blocked this action.")

    if s.bundle_revision:
        lines.append("")
        lines.append(f"Decided under policy bundle {s.bundle_revision}.")
    if s.ledger_seq is not None:
        lines.append(f"Recorded at ledger entry {s.ledger_seq}, before anything was actuated.")

    return "\n".join(lines)


__all__ = ["ACTION_TEXT", "CAUSE_TEXT", "REASON_TEXT", "DecisionSummary", "render_template"]
