"""Razorpay `payment.failed` payloads, hand-built from the documented schema.

Written WITHOUT API keys on purpose. The ingest layer is fully specified by
Razorpay's published webhook schema, so waiting for credentials to build and
test it would have been waiting for nothing. When live captures arrive they
replace the bodies here and every test around them keeps its meaning.

The failure attribution fields are the ones that matter downstream:

    error_code         BAD_REQUEST_ERROR | GATEWAY_ERROR | SERVER_ERROR
    error_source       customer | business | bank | gateway | network | razorpay
    error_step         payment_initiation | payment_authentication |
                       payment_authorization | payment_response
    error_reason       the specific machine-readable reason

`error_source` is the richest merchant-facing attribution field any processor
publishes -- it names who has to act -- and `normalise_razorpay` keeps it
verbatim rather than folding it into the cause.
"""

from __future__ import annotations

import json
from typing import Any

from praman.ingest.signature import compute_signature

# Razorpay's documented header names.
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def failed_payment_event(
    payment_id: str = "pay_TESTdeadlock01",
    amount_paise: int = 2_200_00,
    method: str = "card",
    error_reason: str = "payment_failed",
    error_source: str = "bank",
    error_step: str = "payment_authorization",
    error_code: str = "BAD_REQUEST_ERROR",
    email: str = "customer@example.com",
    contact: str = "+919812345678",
    created_at: int = 1_787_000_000,
) -> dict[str, Any]:
    """One `payment.failed` event.

    Defaults describe the ambiguous case that is the whole product: a bare bank
    refusal, `payment_failed`, which normalises to symbol 05 with NO cause hint
    so the posterior has to do the work.
    """
    entity: dict[str, Any] = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount_paise,
        "currency": "INR",
        "status": "failed",
        "order_id": "order_TEST0000000001",
        "method": method,
        "amount_refunded": 0,
        "captured": False,
        "description": "Subscription renewal",
        "card_id": "card_TEST0000000001",
        "card": {
            "id": "card_TEST0000000001",
            "last4": "1111",
            "network": "Visa",
            "type": "debit",
            "issuer": "HDFC",
            "international": False,
            "sub_type": "consumer",
        },
        "vpa": "customer@okhdfcbank" if method.startswith("upi") else None,
        "email": email,
        "contact": contact,
        "notes": {"merchant_ref": "sub_00042"},
        "fee": None,
        "tax": None,
        "error_code": error_code,
        "error_description": "Your payment was declined by the bank.",
        "error_source": error_source,
        "error_step": error_step,
        "error_reason": error_reason,
        "acquirer_data": {"auth_code": None, "rrn": "224400112233"},
        "created_at": created_at,
    }
    return {
        "entity": "event",
        "account_id": "acc_TEST00000000",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": created_at,
    }


def error_object(event: dict[str, Any]) -> dict[str, Any]:
    """The shape `normalise_razorpay` consumes, lifted out of a webhook body.

    Razorpay flattens the error onto the payment entity in webhooks but nests it
    under `error` in REST responses. Both routes converge here so the normaliser
    has one input shape regardless of how the decline reached us.
    """
    entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    return {
        "code": entity.get("error_code"),
        "description": entity.get("error_description"),
        "source": entity.get("error_source"),
        "step": entity.get("error_step"),
        "reason": entity.get("error_reason"),
    }


def signed_headers(raw_body: bytes, secret: str, event_id: str | None = None) -> dict[str, str]:
    """Headers Razorpay would send for exactly these bytes."""
    if event_id is None:
        try:
            event_id = (
                json.loads(raw_body)
                .get("payload", {})
                .get("payment", {})
                .get("entity", {})
                .get("id", "evt_unknown")
            )
        except (json.JSONDecodeError, AttributeError):
            event_id = "evt_unknown"
    return {
        SIGNATURE_HEADER: compute_signature(raw_body, secret),
        EVENT_ID_HEADER: str(event_id),
        "Content-Type": "application/json",
    }


__all__ = [
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "error_object",
    "failed_payment_event",
    "signed_headers",
]
