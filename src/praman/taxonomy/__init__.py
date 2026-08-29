"""Canonical cause taxonomy and likelihood matrix.

Change C6: the normaliser emits a LIKELIHOOD VECTOR over nine causes, never a
single cause. Codes 05, 61, 62, 65 and Z7 each map to several causes; a
single-cause lookup would destroy exactly the ambiguity that is the product.

The model produces the posterior. The lookup table is the likelihood matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TAXONOMY_PATH = Path(__file__).with_name("canonical_causes.yaml")

# Order is stable and load-bearing: model outputs, ledger rows, and the Bayes
# ceiling all index by it.
CAUSES: tuple[str, ...] = (
    "INSUFFICIENT_FUNDS",
    "ISSUER_RISK_BLOCK",
    "VELOCITY_LIMIT",
    "AUTH_FAILURE",
    "TECHNICAL_DECLINE",
    "EXPIRED_OR_INVALID_CREDENTIAL",
    "INSTRUMENT_DISABLED",
    "LOST_STOLEN_FRAUD",
    "MANDATE_TERMINATED",
)

UNKNOWN_SYMBOL = "UNKNOWN"

# Channels combined as conditionally independent given the cause. Each maps an
# Observation attribute to its block in `side_signals`.
SIDE_CHANNELS: tuple[str, ...] = ("cvv_result", "expiry_valid", "avs_result")


@dataclass(frozen=True, slots=True)
class CauseMeta:
    name: str
    # Exactly what retry.rego's `hard_decline` rule tests: hard == no action of
    # any kind is legal.
    cause_class: str  # "soft" | "hard"
    # Ladder routing, NOT policy. An expired card is not retryable but is
    # perfectly nudgeable, so it is soft + retryable=False + default_tier T3.
    retryable: bool
    default_tier: str  # T0..T4
    description: str


@dataclass(frozen=True, slots=True)
class Observation:
    """One decline, normalised out of whatever vocabulary the processor used.

    `symbol` is the canonical observed code. `cause_hint` is a direct read where
    the processor's own vocabulary is genuinely unambiguous — and is deliberately
    None for catch-all codes like 05, where guessing would rebuild the lookup
    table we set out to replace.
    """

    rail: str
    symbol: str
    raw_code: str | None = None
    processor_reason: str | None = None
    source: str | None = None
    step: str | None = None

    # Orthogonal policy signals. Carried alongside the cause, never folded in.
    network_category: int | None = None
    merchant_advice_code: str | None = None
    npci_retry_remark: str | None = None

    # Side signals (Laumans: CVC, expiry and AVS as clues).
    cvv_result: str | None = None
    expiry_valid: bool | None = None
    avs_result: str | None = None

    cause_hint: str | None = None


class Taxonomy:
    """Immutable view over canonical_causes.yaml."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc
        self._causes = doc["causes"]
        self._priors = doc["priors"]
        self._emissions = doc["emissions"]
        self._side = doc["side_signals"]
        self._symbol_meta = doc.get("symbol_meta", {})

    # ── metadata ────────────────────────────────────────────────────────────
    def cause_meta(self, cause: str) -> CauseMeta:
        c = self._causes[cause]
        return CauseMeta(
            name=cause,
            cause_class=c["class"],
            retryable=bool(c.get("retryable", c["class"] == "soft")),
            default_tier=c["default_tier"],
            description=c["description"],
        )

    def rails(self) -> tuple[str, ...]:
        return tuple(self._emissions)

    def regions(self) -> tuple[str, ...]:
        return tuple(self._priors)

    def symbol_meta(self, symbol: str) -> dict[str, Any]:
        return dict(self._symbol_meta.get(symbol, {}))

    # ── the model ───────────────────────────────────────────────────────────
    def rail_key(self, rail: str) -> str:
        """Map a rail to the emission family it shares.

        `upi_autopay` is a MANDATE executed on the UPI rail -- NPCI returns the
        same response codes -- so it shares UPI's emission matrix rather than
        having one of its own.

        Treating it as a rail in its own right was a real bug with two heads.
        `likelihood()` found no matching block, fell back to the flat "this
        observation carries no information" vector, and discarded the decline
        code for EVERY AutoPay decline: the posterior collapsed to the prior,
        max_posterior 0.26 fell under the 0.40 confidence floor, and the kernel
        refused every automated tier. It also inflated H(C|X) in the Bayes
        ceiling, shrinking the ICR denominator until a trained model could score
        above 1.0 -- extracting more information than the generator says exists.

        Prefix matching against the loaded matrix, not a hardcoded list, so a
        new rail family is a data change. A genuinely unknown rail still yields
        a flat likelihood, which is the correct answer for one.
        """
        if rail in self._emissions:
            return rail
        for known in self._emissions:
            if rail.startswith(known):
                return known
        return rail

    def emissions(self, rail: str, cause: str) -> dict[str, float]:
        """P(symbol | cause, rail)."""
        return dict(self._emissions.get(self.rail_key(rail), {}).get(cause, {}))

    def prior(self, region: str) -> dict[str, float]:
        """P(cause) for a region."""
        return dict(self._priors[region])

    def likelihood(self, obs: Observation) -> dict[str, float]:
        """P(observation | cause) for every cause.

        Unknown symbols and unknown rails yield a flat likelihood rather than
        zeros. Zeroing would make the posterior undefined (0/0); a flat vector
        correctly says "this observation carries no information", so the
        posterior falls back to the prior.
        """
        rail_block = self._emissions.get(self.rail_key(obs.rail), {})
        symbol_is_known = any(obs.symbol in rail_block.get(c, {}) for c in CAUSES)

        out: dict[str, float] = {}
        for cause in CAUSES:
            p = rail_block.get(cause, {}).get(obs.symbol, 0.0) if symbol_is_known else 1.0
            for channel in SIDE_CHANNELS:
                value = getattr(obs, channel, None)
                if value is None:
                    continue
                key = str(value).lower() if isinstance(value, bool) else str(value)
                p *= self._side.get(channel, {}).get(cause, {}).get(key, 1.0)
            out[cause] = p
        return out

    def posterior(self, obs: Observation, region: str = "IN") -> dict[str, float]:
        """P(cause | observation) — normalised prior x likelihood."""
        prior = self.prior(region)
        lik = self.likelihood(obs)
        unnormalised = {c: prior[c] * lik[c] for c in CAUSES}
        total = sum(unnormalised.values())
        if total <= 0.0:
            # Every cause was ruled out. Refuse to invent signal; return prior.
            return prior
        return {c: v / total for c, v in unnormalised.items()}


@lru_cache(maxsize=1)
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    with (path or TAXONOMY_PATH).open("r", encoding="utf-8") as fh:
        return Taxonomy(yaml.safe_load(fh))


__all__ = [
    "CAUSES",
    "SIDE_CHANNELS",
    "UNKNOWN_SYMBOL",
    "CauseMeta",
    "Observation",
    "Taxonomy",
    "load_taxonomy",
]
