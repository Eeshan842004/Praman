"""Synthetic decline generator with SEALED potential outcomes.

The generative direction is the whole point: sample the LATENT CAUSE first, then
sample the observed symbol from the emission matrix in canonical_causes.yaml.
That matrix is the information asymmetry written down, so inverting it is
exactly what attribution does -- and because we authored it, the Bayes-optimal
posterior is analytically computable.

Two things are SEALED and the pipeline must never read them:
    latent_cause   the truth attribution is trying to recover
    y0 / y1        both potential outcomes

Phase 3 extends this with genuinely cause-dependent features (month-end
concentration for insufficient funds, bursty technical declines, and so on) and
a feature-source adapter for IEEE-CIS resampling. The structure here is what it
extends; nothing downstream changes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from praman.taxonomy import CAUSES, load_taxonomy

# P(recovers | no intervention) and P(recovers | intervention), per cause.
# Insufficient funds recovers well on a well-timed retry; a stolen card recovers
# on nothing, and the gap between those two is what a recovery agent is for.
RECOVERY_RATES: dict[str, tuple[float, float]] = {
    #                              y0     y1
    "INSUFFICIENT_FUNDS": (0.15, 0.48),
    "ISSUER_RISK_BLOCK": (0.08, 0.26),
    "VELOCITY_LIMIT": (0.22, 0.46),
    "AUTH_FAILURE": (0.10, 0.34),
    "TECHNICAL_DECLINE": (0.34, 0.71),
    "EXPIRED_OR_INVALID_CREDENTIAL": (0.05, 0.30),
    "INSTRUMENT_DISABLED": (0.04, 0.19),
    "LOST_STOLEN_FRAUD": (0.01, 0.01),  # nothing helps
    "MANDATE_TERMINATED": (0.01, 0.01),  # nothing helps
}

RAILS: tuple[str, ...] = ("card", "upi", "upi_autopay")


@dataclass(frozen=True, slots=True)
class SyntheticDecline:
    payment_id: str
    customer_id: str
    ts_ms: int
    rail: str
    amount_paise: int
    bin: str  # first 6 of the PAN; the velocity key the network actually watches

    # Observed by the pipeline
    symbol: str
    cvv_result: str | None
    expiry_valid: bool | None
    network_category: int | None
    merchant_advice_code: str | None
    npci_retry_remark: str | None
    is_emandate: bool
    afa_completed: bool
    ms_since_pre_debit_notice: int
    ist_hour: int
    has_alternate_instrument: bool

    # Pre-treatment covariate for CUPED, with the timestamp that proves it
    prior_success_rate: float
    covariate_asof_ms: int

    # ── SEALED. Reading these anywhere in src/praman outside sim/ is a bug. ──
    latent_cause: str
    y0_recovered: bool
    y1_recovered: bool


@dataclass(frozen=True, slots=True)
class DeclineBatch:
    declines: list[SyntheticDecline]
    seed: int

    def sealed_truth(self, actioned: dict[str, bool]) -> float:
        """True INTENTION-TO-TREAT effect in paise for this batch.

        Depends on which payments the system would actually act on, so it can
        only be computed once the ladder has run -- but it is still ground
        truth, because both potential outcomes were fixed before any decision
        was made.
        """
        treat, hold = [], []
        for d in self.declines:
            recovered = d.y1_recovered if actioned.get(d.payment_id, False) else d.y0_recovered
            treat.append(d.amount_paise if recovered else 0)
            hold.append(d.amount_paise if d.y0_recovered else 0)
        return float(np.mean(treat) - np.mean(hold))


def _sample_symbol(rng: np.random.Generator, rail: str, cause: str) -> str:
    tax = load_taxonomy()
    rail_key = "upi" if rail.startswith("upi") else "card"
    dist = tax.emissions(rail_key, cause)
    if not dist:
        return "UNKNOWN"
    symbols = list(dist)
    probs = np.array([dist[s] for s in symbols], dtype=float)
    return str(rng.choice(symbols, p=probs / probs.sum()))


def generate_batch(
    n: int = 1000,
    seed: int = 42,
    region: str = "IN",
    customers: int | None = None,
) -> DeclineBatch:
    rng = np.random.default_rng(seed)
    tax = load_taxonomy()

    prior = tax.prior(region)
    causes = list(CAUSES)
    probs = np.array([prior[c] for c in causes], dtype=float)
    probs = probs / probs.sum()

    # Customers repeat. That is what makes clustering real: subscription
    # declines recur per customer, which is precisely why randomising payments
    # instead of customers would violate SUTVA (S7).
    n_customers = customers or max(2, int(n / 2.5))
    base_ts = 1_787_000_000_000

    declines: list[SyntheticDecline] = []
    for i in range(n):
        cause = str(rng.choice(causes, p=probs))
        cust = int(rng.integers(0, n_customers))
        rail = str(rng.choice(RAILS, p=[0.55, 0.30, 0.15]))
        ts = base_ts + i * 60_000

        meta = tax.symbol_meta(symbol := _sample_symbol(rng, rail, cause))

        # Side signals, drawn from the same conditional model attribution uses.
        cvv = None
        expiry = None
        if rail == "card":
            side = tax._side
            cvv_dist = side["cvv_result"][cause]
            cvv = str(rng.choice(list(cvv_dist), p=np.array(list(cvv_dist.values()))))
            exp_dist = side["expiry_valid"][cause]
            expiry = str(rng.choice(list(exp_dist), p=np.array(list(exp_dist.values())))) == "true"

        is_emandate = rail == "upi_autopay" or bool(rng.random() < 0.12)
        prior_success = float(np.clip(rng.beta(6, 3), 0.01, 0.99))

        declines.append(
            SyntheticDecline(
                payment_id=f"pay_SIM{i:07d}",
                customer_id=f"cust_{cust:05d}",
                ts_ms=ts,
                rail=rail,
                amount_paise=int(rng.integers(20_000, 3_000_000)),
                bin=f"{int(rng.integers(0, 40)):06d}",
                symbol=symbol,
                cvv_result=cvv,
                expiry_valid=expiry,
                network_category=meta.get("network_category"),
                merchant_advice_code=meta.get("merchant_advice_code"),
                npci_retry_remark=meta.get("npci_retry_remark"),
                is_emandate=is_emandate,
                afa_completed=bool(rng.random() < 0.35),
                ms_since_pre_debit_notice=int(rng.choice([1_000, 90_000_000])),
                ist_hour=int(rng.integers(0, 24)),
                has_alternate_instrument=bool(rng.random() < 0.6),
                prior_success_rate=prior_success,
                covariate_asof_ms=ts - 86_400_000,  # strictly pre-treatment
                latent_cause=cause,
                y0_recovered=bool(rng.random() < RECOVERY_RATES[cause][0]),
                y1_recovered=bool(rng.random() < RECOVERY_RATES[cause][1]),
            )
        )

    return DeclineBatch(declines=declines, seed=seed)


__all__ = ["RECOVERY_RATES", "DeclineBatch", "SyntheticDecline", "generate_batch"]
