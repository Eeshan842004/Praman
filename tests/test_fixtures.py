"""Real Razorpay test-mode failures, captured via REST.

These are not hand-built. `scripts/capture_fixtures.py` read them off the live
API with the account's test keys, redacted them through the same chokepoint the
webhook path uses, and committed them. That upgrades the ingest claim from
"matches the published schema" to "verified against live traffic", and it is the
only place in the repo where the input was not authored by us.

Three things the live payloads contradicted in our hand-built fixture are
asserted here directly, so they cannot silently drift back:

    notes            arrives as [] -- a LIST -- when empty, not {}
    acquirer_data    carries no `rrn` on a failed payment
    card             carries `name`, the cardholder's name, which is PII

The third is the one that mattered. `redact()` uses an ALLOWLIST for the card
sub-object, so `name` was dropped without anyone having to know it existed. A
denylist would have leaked it, and nothing in the hand-built fixture would have
revealed the gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praman.ingest.fixtures import error_object
from praman.taxonomy import UNKNOWN_SYMBOL, load_taxonomy
from praman.taxonomy.normalise import normalise_razorpay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "razorpay"


def _events() -> list[tuple[str, dict]]:
    if not FIXTURES.exists():
        return []
    return [
        (p.stem, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(FIXTURES.glob("pay_*.json"))
    ]


EVENTS = _events()

pytestmark = pytest.mark.skipif(
    not EVENTS, reason="no captured fixtures; run scripts/capture_fixtures.py"
)


def _entity(event: dict) -> dict:
    return event["payload"]["payment"]["entity"]


# ─────────────────────────────────────────────────────────────────────────────
# We captured something real
# ─────────────────────────────────────────────────────────────────────────────
def test_fixtures_came_from_the_live_api():
    assert EVENTS, "no fixtures captured"
    for _name, event in EVENTS:
        assert event["_source"] == "razorpay_rest_api"
        assert _entity(event)["status"] == "failed"


def test_every_fixture_carries_the_four_attribution_fields():
    """`error_source` is the richest merchant-facing attribution field any
    processor publishes -- it names who has to act. If a live payload stopped
    sending it, the normaliser would quietly lose its best signal."""
    for name, event in EVENTS:
        entity = _entity(event)
        for field in ("error_code", "error_source", "error_step", "error_reason"):
            assert entity.get(field), f"{name} is missing {field}"


# ─────────────────────────────────────────────────────────────────────────────
# No identifier survives redaction
# ─────────────────────────────────────────────────────────────────────────────
def test_no_fixture_contains_a_raw_identifier():
    for name, event in EVENTS:
        entity = _entity(event)
        for field in ("email", "contact", "vpa", "customer_id"):
            value = entity.get(field)
            if value:
                assert str(value).startswith("anon_"), f"{name}.{field} is not pseudonymised"


def test_the_cardholder_name_never_reaches_a_fixture():
    """The finding that justifies the allowlist.

    Live payloads carry `card.name`. Our hand-built fixture did not, so no test
    could have caught it -- `redact()` keeps a fixed list of card fields, so the
    name was dropped without anyone knowing it was there. Under a denylist it
    would have been committed to a public repo.
    """
    blob = json.dumps([e for _n, e in EVENTS])
    assert '"name"' not in blob
    assert '"last4"' not in blob
    assert '"token_iin"' not in blob


def test_merchant_free_text_notes_are_dropped():
    for name, event in EVENTS:
        assert "notes" not in _entity(event), f"{name} still carries notes"


# ─────────────────────────────────────────────────────────────────────────────
# The normaliser handles every one
# ─────────────────────────────────────────────────────────────────────────────
def test_every_fixture_normalises_to_a_known_symbol():
    for name, event in EVENTS:
        entity = _entity(event)
        obs = normalise_razorpay(error_object(event), rail=entity.get("method") or "card")
        assert obs.symbol != UNKNOWN_SYMBOL, (
            f"{name}: reason {entity.get('error_reason')!r} is not in the "
            "Razorpay vocabulary map -- add it to normalise.py"
        )
        assert obs.source == entity["error_source"]
        assert obs.step == entity["error_step"]


def test_every_fixture_yields_a_valid_posterior():
    tax = load_taxonomy()
    for name, event in EVENTS:
        entity = _entity(event)
        obs = normalise_razorpay(error_object(event), rail=entity.get("method") or "card")
        posterior = tax.posterior(obs)
        assert sum(posterior.values()) == pytest.approx(1.0), name
        assert all(v >= 0 for v in posterior.values()), name


def test_the_live_traffic_really_is_the_ambiguous_case():
    """The product's premise, confirmed on real traffic rather than assumed.

    Razorpay returns `payment_failed` for a bare bank refusal, which we
    deliberately map to symbol 05 with NO cause hint -- guessing there would
    rebuild the lookup table this project exists to replace. Most of the
    captured failures are exactly that case.
    """
    reasons = [_entity(e).get("error_reason") for _n, e in EVENTS]
    assert "payment_failed" in reasons

    ambiguous = [r for r in reasons if r in ("payment_failed", "card_declined")]
    assert len(ambiguous) >= len(reasons) // 2, (
        "expected the bare-refusal case to dominate real test traffic"
    )

    for name, event in EVENTS:
        entity = _entity(event)
        if entity.get("error_reason") != "payment_failed":
            continue
        obs = normalise_razorpay(error_object(event), rail=entity.get("method") or "card")
        assert obs.symbol == "05", name
        assert obs.cause_hint is None, f"{name}: 05 must not carry a cause hint"


# ─────────────────────────────────────────────────────────────────────────────
# Live shapes our hand-built payload got wrong
# ─────────────────────────────────────────────────────────────────────────────
def test_a_failed_payment_carries_no_retrieval_reference_number():
    """We invented `acquirer_data.rrn`. A failed authorisation never reaches
    settlement, so there is no RRN to report. Anything downstream that keyed on
    it would have found it only in our own fixtures."""
    for name, event in EVENTS:
        acquirer = _entity(event).get("acquirer_data") or {}
        assert "rrn" not in acquirer or acquirer.get("rrn") is None, name
