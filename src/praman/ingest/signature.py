"""Razorpay webhook signature verification.

    X-Razorpay-Signature = hex(HMAC-SHA256(raw_request_body, webhook_secret))

Two properties, both non-negotiable.

RAW BODY. The signature covers the exact bytes Razorpay sent. Parsing the JSON
and re-serialising it before verifying would change key order and whitespace,
and a signature Razorpay considers valid would be rejected. The handler must
never see a parsed body before this function has seen the raw one.

CONSTANT TIME. `hmac.compare_digest`, never `==`. A short-circuiting comparison
leaks how many leading bytes were correct, which is enough to recover a valid
signature one byte at a time. This endpoint is public by construction -- it has
to be, Razorpay calls it -- so it is the one place in the system where an
attacker controls both the input and the timing.
"""

from __future__ import annotations

import hashlib
import hmac


def compute_signature(raw_body: bytes, secret: str) -> str:
    """The hex digest Razorpay should have sent for this exact body."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, provided: str | None, secret: str) -> bool:
    """True only for a signature that matches these exact bytes.

    Returns False rather than raising on absent, empty or malformed input: an
    exception here would become a 500, and a sender that retries on 5xx would
    turn one malformed request into a redelivery storm.
    """
    if not provided or not secret:
        return False
    try:
        return hmac.compare_digest(compute_signature(raw_body, secret), provided.strip())
    except (TypeError, ValueError, UnicodeError):  # pragma: no cover - defensive
        return False


__all__ = ["compute_signature", "verify_signature"]
