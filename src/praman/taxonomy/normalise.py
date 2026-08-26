"""Normalise heterogeneous processor errors into one canonical Observation.

The portability claim, in one module. The same insufficient-funds decline
surfaces as Stripe `insufficient_funds`, Adyen "Not enough balance", and
Razorpay `insufficient_fund` — three vocabularies, one canonical symbol.

Design rule: where a processor's vocabulary is genuinely unambiguous we record a
`cause_hint`. Where it is a catch-all (05, `card_declined`, `payment_failed`) we
record NO hint and let the posterior do the work. Guessing here would rebuild
the lookup table this project exists to replace.
"""

from __future__ import annotations

from typing import Any

from praman.taxonomy import UNKNOWN_SYMBOL, Observation, load_taxonomy

# ── Razorpay error.reason -> (symbol per rail, cause hint) ──────────────────
# `cause: None` marks a deliberately ambiguous surface.
_RAZORPAY: dict[str, dict[str, Any]] = {
    # Insufficient funds. Razorpay spells it singular for cards and plural for
    # UPI; it is one decline and must normalise to one cause.
    "insufficient_fund": {"card": "51", "upi": "Z9", "cause": "INSUFFICIENT_FUNDS"},
    "insufficient_funds": {"card": "51", "upi": "Z9", "cause": "INSUFFICIENT_FUNDS"},
    # Technical
    "payment_timed_out": {"card": "91", "upi": "U30", "cause": "TECHNICAL_DECLINE"},
    "gateway_technical_error": {"card": "96", "upi": "U28", "cause": "TECHNICAL_DECLINE"},
    "bank_technical_error": {"card": "91", "upi": "U28", "cause": "TECHNICAL_DECLINE"},
    "bank_downtime": {"card": "91", "upi": "U28", "cause": "TECHNICAL_DECLINE"},
    "server_error": {"card": "96", "upi": "U30", "cause": "TECHNICAL_DECLINE"},
    "credit_failed": {"card": "96", "upi": "U30", "cause": "TECHNICAL_DECLINE"},
    # Authentication
    "authentication_failed": {"card": "1A", "upi": "ZM", "cause": "AUTH_FAILURE"},
    "incorrect_cvv": {"card": "82", "upi": "ZM", "cause": "AUTH_FAILURE"},
    "payment_cancelled": {"card": "17", "upi": "ZA", "cause": "AUTH_FAILURE"},
    "payment_collect_request_expired": {"card": "17", "upi": "U69", "cause": "AUTH_FAILURE"},
    # Credential
    "card_expired": {"card": "54", "upi": "U17", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "card_number_invalid": {"card": "14", "upi": "U17", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "invalid_vpa": {"card": "14", "upi": "U17", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "vpa_resolution_failed": {"card": "14", "upi": "U17", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    # Instrument state
    "card_disabled_for_online_payments": {
        "card": "57",
        "upi": "U16",
        "cause": "INSTRUMENT_DISABLED",
    },
    "card_not_enrolled": {"card": "57", "upi": "U16", "cause": "INSTRUMENT_DISABLED"},
    "debit_instrument_inactive": {"card": "57", "upi": "U16", "cause": "INSTRUMENT_DISABLED"},
    "debit_instrument_blocked": {"card": "62", "upi": "U16", "cause": "INSTRUMENT_DISABLED"},
    # Risk / velocity
    "payment_risk_check_failed": {"card": "59", "upi": "U16", "cause": "ISSUER_RISK_BLOCK"},
    "transaction_limit_exceeded": {"card": "61", "upi": "Z8", "cause": "VELOCITY_LIMIT"},
    # ── Deliberately ambiguous: bare bank refusals. NO cause hint. ──────────
    "card_declined": {"card": "05", "upi": "U30", "cause": None},
    "payment_failed": {"card": "05", "upi": "U30", "cause": None},
    "payment_declined": {"card": "05", "upi": "U30", "cause": None},
}

# ── Stripe decline_code -> (ISO symbol, cause hint) ─────────────────────────
_STRIPE: dict[str, dict[str, Any]] = {
    "insufficient_funds": {"symbol": "51", "cause": "INSUFFICIENT_FUNDS"},
    "expired_card": {"symbol": "54", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "invalid_account": {"symbol": "14", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "incorrect_number": {"symbol": "14", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "incorrect_cvc": {"symbol": "82", "cause": "AUTH_FAILURE"},
    "authentication_required": {"symbol": "1A", "cause": "AUTH_FAILURE"},
    "lost_card": {"symbol": "41", "cause": "LOST_STOLEN_FRAUD"},
    "stolen_card": {"symbol": "43", "cause": "LOST_STOLEN_FRAUD"},
    "pickup_card": {"symbol": "04", "cause": "LOST_STOLEN_FRAUD"},
    "fraudulent": {"symbol": "59", "cause": "ISSUER_RISK_BLOCK"},
    "restricted_card": {"symbol": "62", "cause": "INSTRUMENT_DISABLED"},
    "transaction_not_allowed": {"symbol": "57", "cause": "INSTRUMENT_DISABLED"},
    "card_velocity_exceeded": {"symbol": "65", "cause": "VELOCITY_LIMIT"},
    "issuer_not_available": {"symbol": "91", "cause": "TECHNICAL_DECLINE"},
    "processing_error": {"symbol": "96", "cause": "TECHNICAL_DECLINE"},
    # Ambiguous
    "do_not_honor": {"symbol": "05", "cause": None},
    "generic_decline": {"symbol": "05", "cause": None},
    "card_declined": {"symbol": "05", "cause": None},
}

# ── Adyen refusalReason -> (ISO symbol, cause hint) ─────────────────────────
_ADYEN: dict[str, dict[str, Any]] = {
    "not enough balance": {"symbol": "51", "cause": "INSUFFICIENT_FUNDS"},
    "expired card": {"symbol": "54", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "invalid card number": {"symbol": "14", "cause": "EXPIRED_OR_INVALID_CREDENTIAL"},
    "cvc declined": {"symbol": "82", "cause": "AUTH_FAILURE"},
    "3d secure authentication failed": {"symbol": "1A", "cause": "AUTH_FAILURE"},
    "blocked card": {"symbol": "62", "cause": "INSTRUMENT_DISABLED"},
    "restricted card": {"symbol": "62", "cause": "INSTRUMENT_DISABLED"},
    "transaction not permitted": {"symbol": "57", "cause": "INSTRUMENT_DISABLED"},
    "withdrawal count exceeded": {"symbol": "65", "cause": "VELOCITY_LIMIT"},
    "withdrawal amount exceeded": {"symbol": "61", "cause": "VELOCITY_LIMIT"},
    "issuer unavailable": {"symbol": "91", "cause": "TECHNICAL_DECLINE"},
    "acquirer error": {"symbol": "96", "cause": "TECHNICAL_DECLINE"},
    "fraud": {"symbol": "59", "cause": "ISSUER_RISK_BLOCK"},
    # Ambiguous
    "refused": {"symbol": "05", "cause": None},
    "declined": {"symbol": "05", "cause": None},
}


def _decorate(obs_kwargs: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Attach orthogonal policy signals carried by the symbol itself."""
    meta = load_taxonomy().symbol_meta(symbol)
    for field in ("network_category", "merchant_advice_code", "npci_retry_remark"):
        if obs_kwargs.get(field) is None and field in meta:
            obs_kwargs[field] = meta[field]
    return obs_kwargs


def normalise_razorpay(error: dict[str, Any], rail: str = "card") -> Observation:
    """Normalise a Razorpay `error` object from a payment.failed webhook.

    Razorpay's `source` field ("who must act": customer / business / bank /
    gateway / network / razorpay) is the richest merchant-facing attribution
    field any processor exposes. It is preserved verbatim.
    """
    reason = (error or {}).get("reason")
    entry = _RAZORPAY.get(reason or "", {})
    rail_key = "upi" if rail.startswith("upi") else "card"
    symbol = entry.get(rail_key, UNKNOWN_SYMBOL) if entry else UNKNOWN_SYMBOL

    kwargs: dict[str, Any] = {
        "rail": rail,
        "symbol": symbol,
        "raw_code": (error or {}).get("code"),
        "processor_reason": reason,
        "source": (error or {}).get("source"),
        "step": (error or {}).get("step"),
        "cause_hint": entry.get("cause") if entry else None,
    }
    return Observation(**_decorate(kwargs, symbol))


def normalise_stripe(outcome: dict[str, Any], rail: str = "card") -> Observation:
    """Normalise a Stripe charge outcome.

    `network_decline_code` is the raw ISO value and is preferred over the mapped
    `decline_code` whenever present — always trust the rail over the mapping.
    """
    outcome = outcome or {}
    raw = outcome.get("network_decline_code")
    decline_code = outcome.get("decline_code")
    entry = _STRIPE.get(decline_code or "", {})

    symbol = raw or entry.get("symbol") or UNKNOWN_SYMBOL
    kwargs: dict[str, Any] = {
        "rail": rail,
        "symbol": symbol,
        "raw_code": raw or decline_code,
        "processor_reason": decline_code,
        "source": outcome.get("network_status"),
        "merchant_advice_code": outcome.get("network_advice_code"),
        "cause_hint": entry.get("cause") if entry else None,
    }
    return Observation(**_decorate(kwargs, symbol))


def normalise_adyen(response: dict[str, Any], rail: str = "card") -> Observation:
    """Normalise an Adyen refusal.

    `refusalCodeRaw` is the raw numeric response (Adyen-acquired Visa/MC only)
    and is preferred over the mapped `refusalReason` when present.
    """
    response = response or {}
    raw = response.get("refusalCodeRaw")
    reason = response.get("refusalReason")
    entry = _ADYEN.get((reason or "").strip().lower(), {})

    symbol = raw or entry.get("symbol") or UNKNOWN_SYMBOL
    kwargs: dict[str, Any] = {
        "rail": rail,
        "symbol": symbol,
        "raw_code": raw or response.get("refusalReasonRaw"),
        "processor_reason": reason,
        "cause_hint": entry.get("cause") if entry else None,
    }
    return Observation(**_decorate(kwargs, symbol))


__all__ = ["normalise_adyen", "normalise_razorpay", "normalise_stripe"]
