"""The redact() chokepoint.

Every payload that reaches storage passes through here, and nothing reaches
storage another way. One function, so "did this field get redacted?" has exactly
one place to look rather than one place per call site.

PSEUDONYMISE, DO NOT DELETE. The obvious implementation strips email, contact
and VPA outright -- and silently destroys the unit of randomisation. Arms are
assigned per CUSTOMER (law #8, mitigating S7), and a large share of Razorpay
payments carry no `customer_id`, so the contact details ARE the customer key.
Deleting them would scatter one person's payments across both arms, break SUTVA,
and bias every interval toward zero. So each identifier is replaced by a keyed
digest: stable for the same person, unlinkable back to them, and still a usable
cluster key.

The digest is keyed with the webhook secret rather than plain-hashed. The space
of Indian mobile numbers is small enough to enumerate: an unkeyed SHA-256 of a
phone number is reversible by brute force in seconds and is not redaction at
all.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
from typing import Any

PSEUDONYM_PREFIX = "anon_"

# Direct identifiers replaced by a stable keyed digest.
PSEUDONYMISED: tuple[str, ...] = ("email", "contact", "vpa", "customer_id")

# Dropped entirely: merchant-controlled free text, and instrument fragments we
# have no use for. `notes` is the field guaranteed to eventually contain
# something nobody agreed to store.
DROPPED: tuple[str, ...] = ("notes", "card_id", "token_id", "invoice_id")

# Card sub-object: keep what the kernel reasons about, drop the rest. `last4` is
# not needed by any rule and, with an issuer, narrows an individual considerably.
CARD_KEEP: tuple[str, ...] = ("network", "type", "issuer", "international", "sub_type")


def pseudonym(value: Any, salt: str) -> str:
    """A stable, unlinkable stand-in for one identifier."""
    digest = hmac.new(salt.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256)
    return f"{PSEUDONYM_PREFIX}{digest.hexdigest()[:20]}"


def _entity(entity: dict[str, Any], salt: str) -> dict[str, Any]:
    out = {k: v for k, v in entity.items() if k not in DROPPED}

    for field in PSEUDONYMISED:
        if out.get(field):
            out[field] = pseudonym(out[field], salt)

    card = entity.get("card")
    if isinstance(card, dict):
        out["card"] = {k: card[k] for k in CARD_KEEP if k in card}

    return out


def redact(event: dict[str, Any], salt: str) -> dict[str, Any]:
    """Return a storable copy of a Razorpay event. Never mutates the input.

    Not mutating matters: the caller still holds the raw bytes the signature was
    computed over, and quietly editing the object underneath it would make the
    verification unreproducible.
    """
    out = copy.deepcopy(event)
    payload = out.get("payload")
    if not isinstance(payload, dict):
        return out

    for _name, block in payload.items():
        if isinstance(block, dict) and isinstance(block.get("entity"), dict):
            block["entity"] = _entity(block["entity"], salt)
    return out


__all__ = ["CARD_KEEP", "DROPPED", "PSEUDONYMISED", "PSEUDONYM_PREFIX", "pseudonym", "redact"]
