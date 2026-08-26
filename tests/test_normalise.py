"""Normalisation of heterogeneous processor errors into one Observation tuple.

Portability claim under test: the SAME underlying decline, surfaced by three
different processors in three different vocabularies, must resolve to the same
canonical observation. That is the M2 moat, expressed as an assertion.

Written before the implementation exists.
"""

from __future__ import annotations

import pytest

from praman.taxonomy import CAUSES
from praman.taxonomy.normalise import (
    normalise_adyen,
    normalise_razorpay,
    normalise_stripe,
)

# A real Razorpay payment.failed error object, shape-accurate.
RZP_NSF = {
    "code": "BAD_REQUEST_ERROR",
    "description": "Your payment could not be completed due to insufficient account balance.",
    "source": "customer",
    "step": "payment_authorization",
    "reason": "insufficient_fund",
    "metadata": {},
}

RZP_UPI_NSF = {
    "code": "BAD_REQUEST_ERROR",
    "description": "Insufficient balance.",
    "source": "customer",
    "step": "payment_authorization",
    "reason": "insufficient_funds",
    "metadata": {},
}

RZP_GATEWAY = {
    "code": "GATEWAY_ERROR",
    "description": "Payment processing failed due to error at bank or wallet gateway.",
    "source": "gateway",
    "step": "payment_authorization",
    "reason": "gateway_technical_error",
    "metadata": {},
}


# ─────────────────────────────────────────────────────────────────────────────
# Razorpay
# ─────────────────────────────────────────────────────────────────────────────
def test_razorpay_error_object_normalises():
    obs = normalise_razorpay(RZP_NSF, rail="card")
    assert obs.rail == "card"
    assert obs.processor_reason == "insufficient_fund"
    assert obs.source == "customer"
    assert obs.step == "payment_authorization"


def test_razorpay_singular_and_plural_reason_are_the_same_symbol():
    """Razorpay's docs use `insufficient_fund` for cards and `insufficient_funds`
    for UPI. Same decline. The normaliser must not treat them as two things."""
    card = normalise_razorpay(RZP_NSF, rail="card")
    upi = normalise_razorpay(RZP_UPI_NSF, rail="upi")
    assert card.symbol == "51"
    assert upi.symbol == "Z9"
    assert card.cause_hint == upi.cause_hint == "INSUFFICIENT_FUNDS"


def test_razorpay_gateway_error_is_technical():
    obs = normalise_razorpay(RZP_GATEWAY, rail="card")
    assert obs.cause_hint == "TECHNICAL_DECLINE"
    assert obs.source == "gateway"


def test_razorpay_source_is_preserved_verbatim():
    """`source` answers 'who must act' -- the richest merchant-facing attribution
    field any processor exposes. It is carried, never discarded."""
    for payload in (RZP_NSF, RZP_GATEWAY):
        obs = normalise_razorpay(payload, rail="card")
        assert obs.source == payload["source"]


def test_unknown_razorpay_reason_is_total_not_fatal():
    obs = normalise_razorpay({"reason": "reason_invented_next_year"}, rail="card")
    assert obs.symbol is not None
    assert obs.cause_hint is None


# ─────────────────────────────────────────────────────────────────────────────
# ★ Portability: three vocabularies, one canonical observation
# ─────────────────────────────────────────────────────────────────────────────
def test_three_processors_one_canonical_symbol():
    """
    The eight-second demo beat. An insufficient-funds decline surfaced as:
      Stripe    -> decline_code "insufficient_funds", network_decline_code "51"
      Adyen     -> refusalReason "Not enough balance", refusalCodeRaw "51"
      Razorpay  -> reason "insufficient_fund"
    ...must produce one identical canonical symbol.
    """
    rzp = normalise_razorpay(RZP_NSF, rail="card")
    stripe = normalise_stripe(
        {"decline_code": "insufficient_funds", "network_decline_code": "51"}, rail="card"
    )
    adyen = normalise_adyen(
        {"refusalReason": "Not enough balance", "refusalCodeRaw": "51"}, rail="card"
    )
    assert rzp.symbol == stripe.symbol == adyen.symbol == "51"
    assert rzp.cause_hint == stripe.cause_hint == adyen.cause_hint == "INSUFFICIENT_FUNDS"


def test_all_three_normalisers_agree_on_do_not_honor():
    rzp = normalise_razorpay({"reason": "card_declined"}, rail="card")
    stripe = normalise_stripe(
        {"decline_code": "do_not_honor", "network_decline_code": "05"}, rail="card"
    )
    adyen = normalise_adyen({"refusalReason": "Refused", "refusalCodeRaw": "05"}, rail="card")
    assert stripe.symbol == adyen.symbol == "05"
    assert rzp.symbol == "05"


# ─────────────────────────────────────────────────────────────────────────────
# Orthogonal signals: carried ALONGSIDE the cause, never folded into it (§3.2)
# ─────────────────────────────────────────────────────────────────────────────
def test_orthogonal_signals_are_carried_not_collapsed():
    obs = normalise_stripe(
        {
            "decline_code": "do_not_honor",
            "network_decline_code": "05",
            "network_advice_code": "03",
        },
        rail="card",
    )
    assert obs.merchant_advice_code == "03"
    # MAC 03 is a policy input, not a cause. It must not have rewritten the symbol.
    assert obs.symbol == "05"


def test_npci_retry_remark_is_derived_for_upi():
    """NPCI codes carry explicit machine-readable retry semantics that card
    DE-39 simply does not have. This is the India moat, and it belongs on the
    observation so OPA can consume it."""
    obs = normalise_razorpay(RZP_UPI_NSF, rail="upi")
    assert obs.npci_retry_remark in {
        "reinitiate_same_crn",
        "reinitiate_new_crn",
        "do_not_reinitiate",
        "check_status",
    }


def test_cause_hint_is_always_a_known_cause_or_none():
    for payload, rail in (
        (RZP_NSF, "card"),
        (RZP_UPI_NSF, "upi"),
        (RZP_GATEWAY, "card"),
        ({"reason": "nonsense"}, "card"),
    ):
        obs = normalise_razorpay(payload, rail=rail)
        assert obs.cause_hint is None or obs.cause_hint in CAUSES


def test_normalisers_never_raise_on_empty_input():
    for fn in (normalise_razorpay, normalise_stripe, normalise_adyen):
        obs = fn({}, rail="card")
        assert obs.symbol is not None


# ─────────────────────────────────────────────────────────────────────────────
# Fixture-driven: every captured Razorpay failure must normalise
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("reason", "symbol"),
    [
        ("payment_timed_out", "91"),
        ("insufficient_fund", "51"),
        ("payment_cancelled", "17"),
        ("card_declined", "05"),
        ("card_disabled_for_online_payments", "57"),
        ("card_number_invalid", "14"),
        ("gateway_technical_error", "96"),
        ("authentication_failed", "1A"),
    ],
)
def test_all_eight_razorpay_error_test_card_reasons_normalise(reason: str, symbol: str):
    """The eight error test cards are our only real ground truth. Every one of
    them must survive normalisation before we ever fire the real webhook."""
    obs = normalise_razorpay({"reason": reason, "source": "bank"}, rail="card")
    assert obs.symbol == symbol
    assert obs.cause_hint is None or obs.cause_hint in CAUSES


def test_ambiguous_reasons_refuse_to_guess_a_cause():
    """
    `card_declined` is Razorpay's surface for a bare bank refusal -- it maps to
    05, the catch-all. Emitting a confident cause_hint here would rebuild the
    lookup table we set out to replace. The normaliser must decline to guess and
    leave the work to the posterior.
    """
    for reason in ("card_declined", "payment_failed"):
        obs = normalise_razorpay({"reason": reason}, rail="card")
        assert obs.symbol == "05"
        assert obs.cause_hint is None, f"{reason} must not assert a single cause"


def test_unambiguous_reasons_do_provide_a_hint():
    """The converse: where the code really is specific, say so."""
    assert normalise_razorpay({"reason": "card_expired"}, rail="card").cause_hint == (
        "EXPIRED_OR_INVALID_CREDENTIAL"
    )
    assert normalise_razorpay({"reason": "insufficient_fund"}, rail="card").cause_hint == (
        "INSUFFICIENT_FUNDS"
    )
