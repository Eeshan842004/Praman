"""The explanation layer.

Law #1: the LLM never authorises money. It parses and it explains. Nothing here
can change a tier, a deny reason, or an amount -- the decision is already
recorded in the ledger before any of this runs, and the explanation is read from
that record rather than contributing to it.

So the test that matters most is not "is the prose good". It is: does the demo
survive the model being wrong, slow, or absent? The template is computed FIRST
and unconditionally. The LLM is an enhancement layered on top, and every path
where it fails -- timeout, bad JSON, hallucinated cause, invented tier, no API
key at all -- falls back to text that was already rendered.

Written before the implementation exists.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from praman.explain.cache import ArchetypeCache, archetype_key
from praman.explain.service import ExplanationService
from praman.explain.template import DecisionSummary, render_template

DENIED = DecisionSummary(
    payment_id="pay_TESTdeadlock01",
    cause="INSUFFICIENT_FUNDS",
    confidence=0.79,
    tier="T4",
    action="human_escalate",
    amount_paise=22_000_00,
    rail="upi_autopay",
    deny_reasons=["npci_autopay_blackout_window", "rbi_afa_required"],
    tier_evaluations={
        "T1": ["npci_autopay_blackout_window", "rbi_afa_required"],
        "T2": ["no_alternate_instrument"],
        "T3": ["nudge_fatigue_7d"],
        "T4": [],
    },
    bundle_revision="bd45b0c7e5ce66a3",
)

ALLOWED = DecisionSummary(
    payment_id="pay_TEST0000000002",
    cause="TECHNICAL_DECLINE",
    confidence=0.88,
    tier="T1",
    action="silent_retry",
    amount_paise=45_000,
    rail="card",
    deny_reasons=[],
    tier_evaluations={"T1": [], "T2": [], "T3": [], "T4": []},
    bundle_revision="bd45b0c7e5ce66a3",
)


# ─────────────────────────────────────────────────────────────────────────────
# The template always works
# ─────────────────────────────────────────────────────────────────────────────
def test_the_template_needs_no_network_and_no_key():
    text = render_template(DENIED)
    assert text
    assert "INSUFFICIENT_FUNDS".replace("_", " ").lower() in text.lower()


def test_the_template_names_every_blocking_rule():
    """A merchant's question is "why can't you act", and the answer is the
    deny-set. Summarising it away would remove the only thing no incumbent
    shows."""
    text = render_template(DENIED)
    for reason in ("npci_autopay_blackout_window", "rbi_afa_required", "nudge_fatigue_7d"):
        assert reason in text


def test_the_template_states_the_action_actually_taken():
    assert "human" in render_template(DENIED).lower()
    assert "retry" in render_template(ALLOWED).lower()


def test_the_template_reports_money_in_rupees_not_paise():
    assert "22,000" in render_template(DENIED)


def test_the_template_carries_the_bundle_revision():
    """The explanation and the attestation must point at the same policy."""
    assert "bd45b0c7e5ce66a3" in render_template(DENIED)


# ─────────────────────────────────────────────────────────────────────────────
# Archetype cache
# ─────────────────────────────────────────────────────────────────────────────
def test_the_key_ignores_everything_that_does_not_change_the_explanation():
    """Two payments with the same cause, tier, deny-set and confidence band get
    the same words -- so they must share one cache entry and one API call.
    Keying on payment_id would make the cache useless."""
    other = replace(DENIED, payment_id="pay_DIFFERENT", amount_paise=1)
    assert archetype_key(DENIED) == archetype_key(other)


def test_the_key_changes_when_the_deny_set_changes():
    other = replace(DENIED, deny_reasons=["visa_cat1"])
    assert archetype_key(DENIED) != archetype_key(other)


def test_the_key_is_order_independent():
    other = replace(DENIED, deny_reasons=list(reversed(DENIED.deny_reasons)))
    assert archetype_key(DENIED) == archetype_key(other)


def test_confidence_is_bucketed_not_exact():
    """0.79 and 0.81 are the same story. Keying on the raw float would make
    every decision a cache miss and every demo an API call."""
    near = replace(DENIED, confidence=0.77)
    assert archetype_key(DENIED) == archetype_key(near)
    far = replace(DENIED, confidence=0.15)
    assert archetype_key(DENIED) != archetype_key(far)


def test_the_cache_persists_across_connections(tmp_path):
    path = tmp_path / "explain.db"
    ArchetypeCache(path).put("k1", "remembered")
    assert ArchetypeCache(path).get("k1") == "remembered"


# ─────────────────────────────────────────────────────────────────────────────
# The demo never breaks
# ─────────────────────────────────────────────────────────────────────────────
def _service(tmp_path, client=None, **kw):
    return ExplanationService(cache=ArchetypeCache(tmp_path / "c.db"), client=client, **kw)


def test_with_no_client_at_all_the_template_is_returned(tmp_path):
    result = _service(tmp_path).explain(DENIED)
    assert result.source == "template"
    assert result.text == render_template(DENIED)


def test_an_llm_that_raises_falls_back_to_the_template(tmp_path):
    class Boom:
        def complete(self, *_a, **_k):
            raise RuntimeError("gateway timeout")

    result = _service(tmp_path, client=Boom()).explain(DENIED)
    assert result.source == "template"
    assert result.text == render_template(DENIED)


def test_an_llm_that_returns_garbage_falls_back(tmp_path):
    class Garbage:
        def complete(self, *_a, **_k):
            return "not json at all {{{"

    assert _service(tmp_path, client=Garbage()).explain(DENIED).source == "template"


def test_an_llm_that_invents_a_cause_is_rejected(tmp_path):
    """Law #2: an LLM output may narrow authority, never widen it. A model that
    renames the cause is describing a decision that was never made, and the
    ledger -- not the prose -- is the record."""

    class Liar:
        def complete(self, *_a, **_k):
            return json.dumps(
                {"headline": "Card was reported stolen", "detail": "cause: LOST_STOLEN_FRAUD"}
            )

    result = _service(tmp_path, client=Liar()).explain(DENIED)
    assert result.source == "template"
    assert "LOST_STOLEN_FRAUD" not in result.text


def test_an_llm_that_invents_a_tier_is_rejected(tmp_path):
    class Liar:
        def complete(self, *_a, **_k):
            return json.dumps({"headline": "Retried", "detail": "We used T1 silent_retry."})

    result = _service(tmp_path, client=Liar()).explain(DENIED)
    assert result.source == "template"


def test_a_valid_llm_response_is_used_and_cached(tmp_path):
    calls = []

    class Good:
        def complete(self, *_a, **_k):
            calls.append(1)
            return json.dumps(
                {
                    "headline": "We could not retry this payment.",
                    "detail": "Four separate rules blocked every automated option,"
                    " so it went to your ops queue.",
                }
            )

    service = _service(tmp_path, client=Good())
    first = service.explain(DENIED)
    assert first.source == "llm"
    assert "ops queue" in first.text

    second = service.explain(DENIED)
    assert second.source == "cache"
    assert second.text == first.text
    assert len(calls) == 1, "an archetype must cost exactly one API call"


def test_prewarming_populates_the_cache_without_a_later_call(tmp_path):
    """The demo pre-warms so no beat waits on a network round trip."""
    calls = []

    class Good:
        def complete(self, *_a, **_k):
            calls.append(1)
            return json.dumps({"headline": "Blocked.", "detail": "Escalated to a human."})

    service = _service(tmp_path, client=Good())
    service.prewarm([DENIED, ALLOWED])
    assert len(calls) == 2

    assert service.explain(DENIED).source == "cache"
    assert service.explain(ALLOWED).source == "cache"
    assert len(calls) == 2


def test_failures_are_counted_not_swallowed(tmp_path):
    """A silent fallback is indistinguishable from a working integration. The
    metric is how anyone knows the LLM was never actually reached."""
    from praman.metrics import LLM_FALLBACKS

    class Boom:
        def complete(self, *_a, **_k):
            raise RuntimeError("boom")

    before = LLM_FALLBACKS._value.get()
    _service(tmp_path, client=Boom()).explain(DENIED)
    assert LLM_FALLBACKS._value.get() == before + 1


def test_explanation_never_contains_the_word_that_would_imply_authority(tmp_path):
    """The prose describes a decision already recorded. It must not read as
    though the model made it."""
    text = _service(tmp_path).explain(DENIED).text.lower()
    assert "i decided" not in text
    assert "i approved" not in text


@pytest.mark.parametrize("summary", [DENIED, ALLOWED])
def test_every_summary_renders_without_raising(tmp_path, summary):
    assert _service(tmp_path).explain(summary).text


# ─────────────────────────────────────────────────────────────────────────────
# THE LLM NEVER EMITS DIGITS
#
# The bug this section exists for. Explanations are cached per ARCHETYPE, so one
# entry is served to every payment of the same shape -- about 24 of them. A
# model that wrote "your payment of 79.89 rupees" produced prose that was true
# for the payment that minted the entry and WRONG for every other payment it was
# later served to. 15 of 50 shipped archetypes carried an amount.
#
# The critical insight: validating at generation time CANNOT fix this. At that
# moment the number matches the record perfectly. It only becomes false later,
# when the cache serves it to a different payment. So the invariant has to be a
# property of the TEXT ITSELF, independent of which payment it describes:
# archetype-level prose must be payment-invariant, and every digit is a
# payment-specific claim. The template owns every number.
# ─────────────────────────────────────────────────────────────────────────────
def test_a_model_that_emits_an_amount_is_rejected(tmp_path):
    class Numeric:
        def complete(self, *_a, **_k):
            return json.dumps(
                {
                    "headline": "We could not retry this payment.",
                    "detail": "Your payment of 22,000.00 rupees went to the ops queue.",
                }
            )

    result = _service(tmp_path, client=Numeric()).explain(DENIED)
    assert result.source == "template"
    assert "22,000.00 rupees went" not in result.text


def test_a_model_that_emits_any_digit_at_all_is_rejected(tmp_path):
    """Not just amounts. A confidence, a count, a date, a last4 -- every one is
    payment-specific or run-specific, and none of them can be safely frozen into
    prose that will be replayed for a different payment."""
    for detail in (
        "We are 79% confident about the cause.",
        "This is the 3rd attempt on this payment.",
        "The card ending 1111 was declined.",
        "Blocked by 4 separate rules.",
    ):

        class Numeric:
            def __init__(self, d):
                self.d = d

            def complete(self, *_a, **_k):
                return json.dumps({"headline": "Blocked.", "detail": self.d})

        result = _service(tmp_path, client=Numeric(detail)).explain(DENIED)
        assert result.source == "template", f"digit slipped through: {detail!r}"


def test_digit_free_prose_is_still_accepted(tmp_path):
    """The ban must not make the LLM layer useless -- narrative without numbers
    is exactly what it is for."""

    class Wordy:
        def complete(self, *_a, **_k):
            return json.dumps(
                {
                    "headline": "We could not retry this payment.",
                    "detail": "Several separate rules blocked every automated option, "
                    "so it went to your ops queue for a person to handle.",
                }
            )

    assert _service(tmp_path, client=Wordy()).explain(DENIED).source == "llm"


def test_cached_prose_can_never_carry_another_payments_amount(tmp_path):
    """The exact failure, end to end.

    Two payments share an archetype and differ only in amount. The first mints
    the cache entry; the second is served it. The second's rendered explanation
    must not contain the first's amount -- and, because the ban is on digits
    rather than on one field, it cannot contain any of the first's numbers.
    """
    other = replace(DENIED, payment_id="pay_OTHER", amount_paise=7_989_00)
    assert archetype_key(DENIED) == archetype_key(other), "fixtures must share an archetype"

    class LeakyModel:
        def complete(self, *_a, **_k):
            return json.dumps(
                {
                    "headline": "Escalated to your ops queue.",
                    "detail": "Your UPI Autopay transaction of 7,989.00 rupees was blocked.",
                }
            )

    cache = ArchetypeCache(tmp_path / "c.db")
    service = ExplanationService(cache=cache, client=LeakyModel())

    minted = service.explain(other)
    served = service.explain(DENIED)

    # Nothing carrying a digit may ENTER the cache. That is the invariant --
    # the rendered text for a payment legitimately states that payment's own
    # amount, because the TEMPLATE owns numbers and always has.
    assert cache.get(archetype_key(DENIED)) is None, "leaky prose was cached"
    assert minted.source == "template"

    assert "7,989" not in served.text, "another payment's amount leaked through the cache"
    assert "22,000" in served.text, "the template must still state this payment's own amount"
    assert "7,989" in minted.text, "each payment's own template states its own amount"


def test_the_shipped_cache_contains_no_digits():
    """Audits the committed artifact, not just the code path.

    A fix that leaves a poisoned cache in the repo has fixed nothing: the
    dashboard reads the cache, not the validator.
    """
    import re
    import sqlite3
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "explanations.db"
    if not path.exists():
        pytest.skip("no committed explanation cache")

    conn = sqlite3.connect(str(path))
    try:
        offenders = [
            (key, sorted(set(re.findall(r"[0-9][0-9,.]*", text))))
            for key, text in conn.execute("SELECT key, text FROM explanations")
            if re.search(r"[0-9]", text)
        ]
    finally:
        conn.close()

    assert not offenders, f"{len(offenders)} cached archetypes carry digits: {offenders[:3]}"
