"""Synthetic decline generator with cause-dependent features and SEALED outcomes.

The generative order is what makes this non-vacuous:

    1. resample base features from a FeatureSource (real marginals)
    2. lay them on a timeline; derive month position, outages, velocity, and how
       far each amount deviates from that customer's own history
    3. sample the LATENT CAUSE conditioned on those features
    4. sample the observed SYMBOL from the emission matrix given the cause
    5. sample both potential outcomes given the cause and the customer

Step 3 is the point. If the cause were drawn independently of the features, the
symbol would be the only information in existence: Bayes would extract it
analytically, a trained model would learn nothing on top, and the Information
Capture Ratio would come out ~1.0 for free -- a headline number meaning nothing.
Real declines are not like that. Insufficient funds clusters at month-end,
downtime arrives in bursts, risk blocks follow unusual amounts.
tests/test_features.py asserts each of those against a shuffled control.

SEALED, and never read outside this module:
    latent_cause, y0_recovered, y1_recovered
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from praman.sim.features import FeatureSource, default_feature_source
from praman.taxonomy import CAUSES, load_taxonomy

# P(recovers | no intervention), P(recovers | intervention). The gap between the
# two is what a recovery agent is for -- and it is zero for a stolen card.
RECOVERY_RATES: dict[str, tuple[float, float]] = {
    "INSUFFICIENT_FUNDS": (0.15, 0.48),
    "ISSUER_RISK_BLOCK": (0.08, 0.26),
    "VELOCITY_LIMIT": (0.22, 0.46),
    "AUTH_FAILURE": (0.10, 0.34),
    "TECHNICAL_DECLINE": (0.34, 0.71),
    "EXPIRED_OR_INVALID_CREDENTIAL": (0.05, 0.30),
    "INSTRUMENT_DISABLED": (0.04, 0.19),
    "LOST_STOLEN_FRAUD": (0.01, 0.01),
    "MANDATE_TERMINATED": (0.01, 0.01),
}

RAILS: tuple[str, ...] = ("card", "upi", "upi_autopay")

DAY_MS = 86_400_000
SPAN_DAYS = 60
BASE_TS = 1_787_000_000_000

# Outage shape. Tuned so the technical-decline arrival process is clearly
# over-dispersed (Fano >> 1), which is what tests/test_features.py checks.
N_OUTAGES = 9
OUTAGE_HOURS = (5, 16)

# Attempt bursts on a single BIN: retry storms and card testing.
N_BINS = 40
N_BURSTS = 26
BURST_WINDOW_MS = 3 * 3_600_000


@dataclass(frozen=True, slots=True)
class SyntheticDecline:
    payment_id: str
    customer_id: str
    ts_ms: int
    rail: str
    amount_paise: int
    bin: str

    # ── Observable features (the model may read every one of these) ────────
    hour_of_day: int
    day_of_month: int
    days_since_payday: int
    attempts_prior_1h: int
    in_outage: int
    amount_z: float
    customer_prior_success: float
    network: str
    funding: str

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

    cuped_covariate: float
    covariate_asof_ms: int

    # ── SEALED ─────────────────────────────────────────────────────────────
    latent_cause: str
    y0_recovered: bool
    y1_recovered: bool


@dataclass(frozen=True, slots=True)
class DeclineBatch:
    declines: list[SyntheticDecline]
    seed: int
    # SEALED. P(cause | features) as actually sampled from, one row per decline.
    # The Bayes-optimal posterior is this times the emission and side-signal
    # likelihoods -- so the information ceiling is COMPUTED, not approximated.
    # Recomputing it downstream would turn an exact ratio into an estimate.
    cause_probs: np.ndarray = None  # type: ignore[assignment]

    def sealed_truth(self, actioned: dict[str, bool]) -> float:
        """True intention-to-treat effect in paise for this batch."""
        treat, hold = [], []
        for d in self.declines:
            recovered = d.y1_recovered if actioned.get(d.payment_id, False) else d.y0_recovered
            treat.append(d.amount_paise if recovered else 0)
            hold.append(d.amount_paise if d.y0_recovered else 0)
        return float(np.mean(treat) - np.mean(hold))


def _cause_tilts(
    days_since_payday: np.ndarray,
    attempts_prior_1h: np.ndarray,
    in_outage: np.ndarray,
    amount_z: np.ndarray,
) -> np.ndarray:
    """Multiplicative tilt on the prior, per cause, per row.

    Each factor encodes one documented regularity. Strong enough to be
    detectable, weak enough that the marginal mix survives the correction below.
    """
    n = days_since_payday.size
    tilt = np.ones((n, len(CAUSES)))
    idx = {c: i for i, c in enumerate(CAUSES)}

    # Wallets empty as the month runs on, and refill on payday.
    tilt[:, idx["INSUFFICIENT_FUNDS"]] = np.exp(1.9 * (days_since_payday / 30.0 - 0.5))
    # Velocity ceilings are hit by repeated attempts, by definition.
    tilt[:, idx["VELOCITY_LIMIT"]] = np.exp(0.95 * np.minimum(attempts_prior_1h, 6))
    # Downtime is bursty: inside an outage, technical declines dominate.
    tilt[:, idx["TECHNICAL_DECLINE"]] = np.exp(3.2 * in_outage)
    # Issuer risk engines fire on amounts unlike the customer's own history.
    tilt[:, idx["ISSUER_RISK_BLOCK"]] = np.exp(0.85 * np.minimum(np.abs(amount_z), 3.0))
    return tilt


def _sample_causes(
    rng: np.random.Generator, prior: dict[str, float], tilt: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Draw causes from the tilted prior, corrected so the MARGINAL still tracks
    the regional prior.

    Tilting alone would quietly rewrite the very prior the posterior is
    calibrated against. A short fixed-point iteration rescales the base until the
    realised mix matches: structure gets added, calibration does not get spent.
    """
    target = np.array([prior[c] for c in CAUSES], dtype=float)
    base = target.copy()

    for _ in range(12):
        probs = base * tilt
        probs /= probs.sum(axis=1, keepdims=True)
        base *= target / np.maximum(probs.mean(axis=0), 1e-9)
        base /= base.sum()

    probs = base * tilt
    probs /= probs.sum(axis=1, keepdims=True)

    u = rng.random(probs.shape[0])[:, None]
    drawn = (probs.cumsum(axis=1) < u).sum(axis=1).clip(0, len(CAUSES) - 1)
    return drawn, probs


def generate_batch(
    n: int = 1000,
    seed: int = 42,
    region: str = "IN",
    customers: int | None = None,
    feature_source: FeatureSource | None = None,
) -> DeclineBatch:
    rng = np.random.default_rng(seed)
    tax = load_taxonomy()
    base = (feature_source or default_feature_source()).sample(n, rng)

    # ── Timeline ───────────────────────────────────────────────────────────
    ts = np.sort(rng.integers(0, SPAN_DAYS * DAY_MS, size=n)) + BASE_TS
    day_of_month = (((ts - BASE_TS) // DAY_MS) % 30).astype(int) + 1
    # Salaries land on the 1st; pressure builds through the month.
    days_since_payday = day_of_month - 1

    # ── Outage windows: downtime is bursty, not Poisson ────────────────────
    # Real bank downtime lasts hours, not minutes, and takes a visible bite out
    # of a day. Short windows spread the effect thinly enough that the resulting
    # arrival process is statistically indistinguishable from Poisson -- which
    # would misrepresent the one cause whose entire character is that it arrives
    # all at once.
    in_outage = np.zeros(n, dtype=int)
    for _ in range(N_OUTAGES):
        start = int(rng.integers(0, SPAN_DAYS * DAY_MS)) + BASE_TS
        width = int(rng.integers(*OUTAGE_HOURS)) * 3_600_000
        in_outage |= ((ts >= start) & (ts < start + width)).astype(int)

    # ── Customers, with a latent reliability that drives recovery ──────────
    n_customers = customers or max(4, int(n / 2.5))
    cust = rng.integers(0, n_customers, size=n)
    reliability = rng.beta(6, 3, size=n_customers)[cust]
    # What a merchant can actually observe pre-treatment: a noisy read of it.
    observed_success = np.clip(reliability + rng.normal(0, 0.08, n), 0.01, 0.99)

    # ── Amounts scaled per customer, so deviation is meaningful ────────────
    cust_scale = np.exp(rng.normal(0, 0.45, size=n_customers))[cust]
    typical = base["amount_paise"] * cust_scale
    anomalous = rng.random(n) < 0.08
    amount = np.clip(
        typical * np.where(anomalous, rng.uniform(3, 9, n), 1.0), 5_000, 9_000_000
    ).astype(np.int64)
    amount_z = (np.log(amount) - np.log(typical)) / 0.6

    # ── BIN velocity in the prior hour: from the timeline only ─────────────
    # Attempts on a BIN are not uniform. Retry storms and card-testing arrive as
    # bursts on ONE bin in a short window, which is exactly the condition a
    # velocity ceiling exists to catch. Spread uniformly across 40 bins over 60
    # days, the prior-hour count would be ~0 for every row and the feature would
    # carry no information at all.
    bins = np.array([f"{b:06d}" for b in rng.integers(0, N_BINS, size=n)])
    for _ in range(N_BURSTS):
        hot = f"{int(rng.integers(0, N_BINS)):06d}"
        start = int(rng.integers(0, SPAN_DAYS * DAY_MS)) + BASE_TS
        window = (ts >= start) & (ts < start + BURST_WINDOW_MS)
        bins[window & (rng.random(n) < 0.85)] = hot
    attempts_prior_1h = np.zeros(n, dtype=int)
    for b in np.unique(bins):
        mask = np.flatnonzero(bins == b)
        bt = ts[mask]
        attempts_prior_1h[mask] = [int(((bt >= t - 3_600_000) & (bt < t)).sum()) for t in bt]

    # ── Cause CONDITIONED on the features above ────────────────────────────
    cause_idx, cause_probs = _sample_causes(
        rng,
        tax.prior(region),
        _cause_tilts(days_since_payday, attempts_prior_1h, in_outage, amount_z),
    )

    rails = rng.choice(RAILS, size=n, p=[0.55, 0.30, 0.15])
    side = tax._side

    declines: list[SyntheticDecline] = []
    for i in range(n):
        cause = CAUSES[cause_idx[i]]
        rail = str(rails[i])
        rail_key = "upi" if rail.startswith("upi") else "card"

        dist = tax.emissions(rail_key, cause)
        symbols = list(dist)
        p = np.array([dist[s] for s in symbols], dtype=float)
        symbol = str(rng.choice(symbols, p=p / p.sum())) if symbols else "UNKNOWN"
        meta = tax.symbol_meta(symbol)

        cvv = expiry = None
        if rail == "card":
            cd = side["cvv_result"][cause]
            cvv = str(rng.choice(list(cd), p=np.array(list(cd.values()))))
            ed = side["expiry_valid"][cause]
            expiry = str(rng.choice(list(ed), p=np.array(list(ed.values())))) == "true"

        # Reliability modulates recovery, which is what makes the observed
        # success rate a genuinely predictive CUPED covariate rather than noise.
        y0_rate, y1_rate = RECOVERY_RATES[cause]
        scale = 0.30 + 1.45 * float(reliability[i])

        declines.append(
            SyntheticDecline(
                payment_id=f"pay_SIM{i:07d}",
                customer_id=f"cust_{cust[i]:05d}",
                ts_ms=int(ts[i]),
                rail=rail,
                amount_paise=int(amount[i]),
                bin=str(bins[i]),
                hour_of_day=int(base["hour_of_day"][i]),
                day_of_month=int(day_of_month[i]),
                days_since_payday=int(days_since_payday[i]),
                attempts_prior_1h=int(attempts_prior_1h[i]),
                in_outage=int(in_outage[i]),
                amount_z=float(amount_z[i]),
                customer_prior_success=float(observed_success[i]),
                network=str(base["network"][i]),
                funding=str(base["funding"][i]),
                symbol=symbol,
                cvv_result=cvv,
                expiry_valid=expiry,
                network_category=meta.get("network_category"),
                merchant_advice_code=meta.get("merchant_advice_code"),
                npci_retry_remark=meta.get("npci_retry_remark"),
                is_emandate=rail == "upi_autopay" or bool(rng.random() < 0.12),
                afa_completed=bool(rng.random() < 0.35),
                ms_since_pre_debit_notice=int(rng.choice([1_000, 90_000_000])),
                ist_hour=int(base["hour_of_day"][i]),
                has_alternate_instrument=bool(rng.random() < 0.6),
                # Expected recovery: amount x observed reliability. Both strictly
                # pre-treatment, and together they predict the outcome -- which
                # is the entire requirement for CUPED to reduce any variance.
                cuped_covariate=float(amount[i] * observed_success[i]),
                covariate_asof_ms=int(ts[i]) - DAY_MS,
                latent_cause=cause,
                y0_recovered=bool(rng.random() < min(y0_rate * scale, 0.98)),
                y1_recovered=bool(rng.random() < min(y1_rate * scale, 0.98)),
            )
        )

    return DeclineBatch(declines=declines, seed=seed, cause_probs=cause_probs)


__all__ = ["RECOVERY_RATES", "DeclineBatch", "SyntheticDecline", "generate_batch"]
