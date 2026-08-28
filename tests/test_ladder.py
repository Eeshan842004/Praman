"""Escalation ladder T0-T4.

The rule that matters: evaluate EVERY tier and never short-circuit. The value of
the regulatory-deadlock case is the complete conflict matrix -- four regulators
each forbidding a different tier of the same payment -- and a `for tier in tiers:
if allowed: return` loop throws exactly that away.

T1/T2/T3 are actions requiring authorisation. T0 (terminate) and T4 (escalate to
a human) are not money actions: doing nothing needs no permission, and neither
does asking a person.

Written before the implementation exists.
"""

from __future__ import annotations

import httpx
import pytest

from praman.kernel.ladder import (
    ACTIONABLE_TIERS,
    TIER_ACTION,
    DeclineContext,
    build_policy_input,
    evaluate_ladder,
)
from praman.kernel.opa_client import PolicyClient


def _ctx(**kw) -> DeclineContext:
    base = dict(
        cause="INSUFFICIENT_FUNDS",
        max_posterior=0.81,
        rail="card",
        amount_paise=50_000,
        network_category=2,
        merchant_advice_code="01",
        npci_retry_remark="reinitiate_same_crn",
        attempts_30d=3,
        attempts_this_payment=1,
        bin_attempts_1h=2,
        customer_nudges_7d=0,
        is_emandate=False,
        afa_completed=False,
        ms_since_pre_debit_notice=999_999_999,
        ist_hour=15,
        has_alternate_instrument=True,
    )
    base.update(kw)
    return DeclineContext(**base)


def _client(handler) -> PolicyClient:
    return PolicyClient(transport=httpx.MockTransport(handler))


def _always(allow: bool, reasons: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decision_id": "d",
                "result": {
                    "allow": allow,
                    "deny_reason": reasons or ([] if allow else ["blocked"]),
                    "bundle_revision": "rev1",
                },
            },
        )

    return handler


def _per_tier(allowed: set[str]):
    """Allow only the named tiers; deny the rest with a tier-specific reason."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        tier = json.loads(request.content)["input"]["tier"]
        ok = tier in allowed
        return httpx.Response(
            200,
            json={
                "decision_id": "d",
                "result": {
                    "allow": ok,
                    "deny_reason": [] if ok else [f"denied_{tier.lower()}"],
                    "bundle_revision": "rev1",
                },
            },
        )

    return handler


# ─────────────────────────────────────────────────────────────────────────────
# Policy input construction
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_input_carries_every_field_the_rego_reads():
    required = {
        "cause_class",
        "network_category",
        "merchant_advice_code",
        "npci_retry_remark",
        "attempts_30d",
        "attempts_this_payment",
        "bin_attempts_1h",
        "tier",
        "customer_nudges_7d",
        "is_emandate",
        "amount_paise",
        "afa_completed",
        "ms_since_pre_debit_notice",
        "rail",
        "ist_hour",
        "max_posterior",
        "has_alternate_instrument",
    }
    assert required <= set(build_policy_input(_ctx(), "T1"))


def test_max_posterior_is_a_six_dp_string_not_a_float():
    """The rego does to_number() on it, and the ledger hashes it. A raw float
    would be both a type mismatch and a law #5 violation."""
    assert build_policy_input(_ctx(max_posterior=0.8134), "T1")["max_posterior"] == "0.813400"


def test_cause_class_comes_from_the_taxonomy_not_the_caller():
    assert build_policy_input(_ctx(cause="LOST_STOLEN_FRAUD"), "T1")["cause_class"] == "hard"
    assert build_policy_input(_ctx(cause="INSUFFICIENT_FUNDS"), "T1")["cause_class"] == "soft"


# ─────────────────────────────────────────────────────────────────────────────
# No short-circuiting
# ─────────────────────────────────────────────────────────────────────────────
def test_every_tier_is_evaluated_even_when_the_first_one_allows():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content)["input"]["tier"])
        return httpx.Response(
            200, json={"result": {"allow": True, "deny_reason": [], "bundle_revision": "r"}}
        )

    evaluate_ladder(_ctx(), _client(handler))
    assert set(calls) >= set(ACTIONABLE_TIERS)


def test_outcome_records_an_evaluation_for_every_queried_tier():
    out = evaluate_ladder(_ctx(), _client(_always(True)))
    assert set(out.evaluations) >= set(ACTIONABLE_TIERS)
    for ev in out.evaluations.values():
        assert isinstance(ev.deny_reasons, list)


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────
def test_default_tier_is_chosen_when_policy_allows_it():
    out = evaluate_ladder(_ctx(cause="INSUFFICIENT_FUNDS"), _client(_always(True)))
    assert out.selected_tier == "T1"
    assert out.is_action is True


def test_escalates_past_a_denied_default_tier():
    out = evaluate_ladder(_ctx(cause="INSUFFICIENT_FUNDS"), _client(_per_tier({"T2", "T3", "T4"})))
    assert out.selected_tier == "T2"
    assert out.evaluations["T1"].allow is False


def test_falls_through_to_human_escalation_when_no_action_is_permitted():
    out = evaluate_ladder(_ctx(), _client(_per_tier({"T4"})))
    assert out.selected_tier == "T4"
    assert out.is_action is False


def test_terminates_when_even_escalation_is_denied():
    out = evaluate_ladder(_ctx(), _client(_per_tier(set())))
    assert out.selected_tier == "T0"
    assert out.is_action is False


# ─────────────────────────────────────────────────────────────────────────────
# Retryability vs cause class -- the distinction the taxonomy now makes
# ─────────────────────────────────────────────────────────────────────────────
def test_a_hard_cause_terminates_without_proposing_any_action():
    """Lost/stolen means nothing is legal. We must not even ask."""
    out = evaluate_ladder(_ctx(cause="LOST_STOLEN_FRAUD"), _client(_always(True)))
    assert out.selected_tier == "T0"
    assert out.is_action is False


@pytest.mark.parametrize("cause", ["EXPIRED_OR_INVALID_CREDENTIAL", "INSTRUMENT_DISABLED"])
def test_non_retryable_causes_never_propose_a_retry(cause: str):
    """An expired card cannot be retried, but it CAN be nudged. Routing it to
    terminate would throw away a genuinely recoverable payment."""
    out = evaluate_ladder(_ctx(cause=cause), _client(_always(True)))
    assert out.selected_tier == "T3"
    assert "T1" not in out.proposed_tiers
    assert "T2" not in out.proposed_tiers


def test_retryable_causes_do_propose_a_retry():
    out = evaluate_ladder(_ctx(cause="INSUFFICIENT_FUNDS"), _client(_always(True)))
    assert "T1" in out.proposed_tiers


# ─────────────────────────────────────────────────────────────────────────────
# LAW #9 -- OPA down means escalate, never act
# ─────────────────────────────────────────────────────────────────────────────
def test_unreachable_opa_escalates_to_a_human():
    out = evaluate_ladder(_ctx(), PolicyClient(base_url="http://127.0.0.1:1"))
    assert out.selected_tier == "T0"
    assert out.is_action is False
    assert all(ev.failed_closed for ev in out.evaluations.values())
    assert all("opa_unavailable" in ev.deny_reasons for ev in out.evaluations.values())


# ─────────────────────────────────────────────────────────────────────────────
# S3 -- the regulatory deadlock, end to end through the ladder
# ─────────────────────────────────────────────────────────────────────────────
def test_regulatory_deadlock_preserves_the_full_conflict_matrix():
    handler = _per_tier({"T4"})
    out = evaluate_ladder(
        _ctx(
            cause="INSUFFICIENT_FUNDS",
            rail="upi_autopay",
            is_emandate=True,
            amount_paise=2_200_000,
            ist_hour=11,
            customer_nudges_7d=2,
            ms_since_pre_debit_notice=1_000,
            has_alternate_instrument=False,
        ),
        _client(handler),
    )
    assert out.selected_tier == "T4"
    # Every denied tier kept its own reason; nothing was collapsed or skipped.
    assert out.evaluations["T1"].deny_reasons == ["denied_t1"]
    assert out.evaluations["T2"].deny_reasons == ["denied_t2"]
    assert out.evaluations["T3"].deny_reasons == ["denied_t3"]


def test_tier_action_names_are_stable_for_the_audit_trail():
    assert TIER_ACTION["T0"] == "terminate"
    assert TIER_ACTION["T1"] == "silent_retry"
    assert TIER_ACTION["T2"] == "rail_switch"
    assert TIER_ACTION["T3"] == "customer_nudge"
    assert TIER_ACTION["T4"] == "human_escalate"
