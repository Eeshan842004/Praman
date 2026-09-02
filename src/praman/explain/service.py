"""Template first, model second, ledger always.

The order is the design. `render_template` runs unconditionally and its output
is what gets returned unless a model response survives every check below. There
is no path where a failed, slow, malformed or hallucinated LLM response leaves
the caller without an explanation.

LAW #1 AND #2 ARE ENFORCED HERE, NOT ASSUMED. The model receives a decision that
has already been made, recorded and attested. It cannot change a tier, a deny
reason or an amount, because nothing it returns is parsed as one -- its output
is prose, and prose that mentions a cause or tier other than the recorded ones
is REJECTED rather than trusted. An LLM output may narrow authority; it may
never widen it. Here it holds no authority at all.

Validation is done in Python regardless of what the gateway claims to support.
Proxy structured-output support is unreliable, so "the API validated it" is not
a fact this code is willing to depend on.

THE MODEL NEVER EMITS DIGITS. The template owns every number.

This is the invariant the first version was missing, and it shipped a wrong
amount to a merchant-facing page. Explanations are cached per ARCHETYPE, so one
entry is served to every payment of the same shape -- roughly 24 of them. A
model that wrote "your payment of 79.89 rupees" produced prose that was true for
the payment that minted the entry and FALSE for every other payment served it.
Fifteen of fifty shipped archetypes carried an amount.

The insight that determines the fix: validating the number against the decision
record at GENERATION time cannot help. At that moment it matches perfectly. It
only becomes false later, when the cache serves it to a different payment. So
the invariant has to be a property of the text itself, independent of which
payment it describes -- and since an archetype is by definition the
payment-invariant part of the story, every digit is a payment-specific claim
that does not belong in it.

Adding the amount to the cache key would also "work" and would be the wrong
trade: it turns fifty API calls into twelve hundred and destroys the archetype
design. The numbers were never the model's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from praman.explain.cache import ArchetypeCache, archetype_key
from praman.explain.template import DecisionSummary, render_template
from praman.metrics import LLM_CACHE_HITS, LLM_CACHE_MISSES, LLM_FALLBACKS
from praman.taxonomy import CAUSES

# Every tier token a model could name. Mentioning one that was not the recorded
# tier means it is describing a different decision.
TIERS: tuple[str, ...] = ("T0", "T1", "T2", "T3", "T4")

MAX_CHARS = 900


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Explanation:
    text: str
    source: str  # "template" | "llm" | "cache"
    archetype: str = ""


SYSTEM_PROMPT = (
    "You explain a payment-recovery decision that has ALREADY been made and "
    "recorded. You are not making the decision and you must not suggest a "
    "different one. Write for a merchant, not an engineer.\n\n"
    "Return ONLY a JSON object with exactly two string keys:\n"
    '  "headline" - one sentence, under 90 characters\n'
    '  "detail"   - two or three sentences of plain English\n\n'
    "HARD CONSTRAINT: write NO DIGITS AT ALL. No amounts, no percentages, no "
    "counts, no dates, no card numbers, no tier codes. Your text is reused for "
    "many payments that differ in amount, so any number you write would be "
    "wrong for most of them. The interface prints every number itself -- say "
    '"this payment" and "several rules", never a figure. Text containing '
    "a digit is discarded.\n\n"
    "Never name a cause or tier other than the ones given. Never claim an "
    "action other than the one given."
)


def _prompt(s: DecisionSummary) -> str:
    """Only the ARCHETYPE, as data. No instructions from the payload.

    Deliberately excludes the amount, the payment id and the raw confidence.
    Two reasons, and the second is the load-bearing one:

      * the prompt should contain exactly what the cache key contains, or the
        model is being asked to write prose about facts that vary across the
        payments the entry will be served to;
      * a model cannot echo a number it was never given. The digit check below
        is the guarantee; not sending the number is what stops the model
        tripping it on almost every call.

    Confidence is passed as a BAND rather than a figure for the same reason.
    """
    return json.dumps(
        {
            "cause": s.cause,
            "tier": s.tier,
            "action_taken": s.action,
            "rail": s.rail,
            "how_confident": (
                "confident"
                if s.confidence >= 0.7
                else "fairly sure"
                if s.confidence >= 0.4
                else "unsure"
            ),
            "rules_that_blocked_each_tier": {k: sorted(v) for k, v in s.tier_evaluations.items()},
        },
        sort_keys=True,
    )


def _validate(raw: str, s: DecisionSummary) -> str | None:
    """Return usable prose, or None to fall back.

    Rejects on: unparseable JSON, wrong shape, over-length, a cause that is not
    the recorded one, or a tier that is not the recorded one. The last two are
    the ones that matter -- a model renaming the cause is describing a decision
    that never happened, and the ledger is the record, not the prose.
    """
    try:
        payload: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None
    headline, detail = payload.get("headline"), payload.get("detail")
    if not isinstance(headline, str) or not isinstance(detail, str):
        return None
    if not headline.strip() or not detail.strip():
        return None

    text = f"{headline.strip()}\n\n{detail.strip()}"
    if len(text) > MAX_CHARS:
        return None

    upper = text.upper()
    for cause in CAUSES:
        if cause != s.cause and cause in upper:
            return None
    for tier in TIERS:
        if tier != s.tier and tier in text:
            return None

    # Every digit is a payment-specific claim, and this prose will be replayed
    # for other payments that share its archetype. No exception for "the number
    # happens to be right" -- it is right exactly once, for the payment that
    # minted the entry, and wrong for the ~24 served it afterwards.
    if any(ch.isdigit() for ch in text):
        return None

    return text


class ExplanationService:
    """Explain a recorded decision. Never fails, never blocks the demo."""

    __slots__ = ("_cache", "_client")

    def __init__(self, cache: ArchetypeCache, client: LLMClient | None = None) -> None:
        self._cache = cache
        self._client = client

    def explain(self, summary: DecisionSummary) -> Explanation:
        # Computed first and unconditionally. Everything below is an upgrade on
        # text that is already correct.
        fallback = render_template(summary)
        key = archetype_key(summary)

        cached = self._cache.get(key)
        if cached:
            LLM_CACHE_HITS.inc()
            return Explanation(cached, "cache", key)

        LLM_CACHE_MISSES.inc()
        if self._client is None:
            return Explanation(fallback, "template", key)

        try:
            raw = self._client.complete(SYSTEM_PROMPT, _prompt(summary))
        except Exception:
            # Any failure at all -- timeout, auth, rate limit, gateway error.
            # The metric is how anyone knows the model was never reached; a
            # silent fallback is indistinguishable from a working integration.
            LLM_FALLBACKS.inc()
            return Explanation(fallback, "template", key)

        text = _validate(raw, summary)
        if text is None:
            LLM_FALLBACKS.inc()
            return Explanation(fallback, "template", key)

        self._cache.put(key, text)
        return Explanation(text, "llm", key)

    def prewarm(self, summaries: list[DecisionSummary]) -> int:
        """Fill the cache before a demo so no beat waits on a round trip.

        Returns the number of archetypes newly populated. Distinct archetypes
        only -- a thousand payments of the same shape cost one call.
        """
        seen: set[str] = set()
        filled = 0
        for summary in summaries:
            key = archetype_key(summary)
            if key in seen or self._cache.get(key):
                continue
            seen.add(key)
            if self.explain(summary).source == "llm":
                filled += 1
        return filled


__all__ = ["MAX_CHARS", "SYSTEM_PROMPT", "TIERS", "Explanation", "ExplanationService", "LLMClient"]
