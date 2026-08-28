"""Feature sources behind an adapter.

Kaggle must never be able to block the build, so the generator depends on this
interface rather than on a dataset. `SyntheticFeatureSource` always works;
`IEEECISFeatureSource` reads a small committed parquet distilled from IEEE-CIS
and is used automatically when present. Swapping one for the other moves
nothing downstream -- the same pattern that let the estimator harness be built
and validated before the simulator existed.

Why resample real features at all: published work (arXiv, *Synthetic Tabular
Generators Fail to Preserve Behavioral Fraud Patterns*) shows generators break
temporal, velocity and multi-account structure. So we do not synthesise
features. We resample real feature ROWS and synthesise only the causal label
layer on top of them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

ARTIFACT = Path(__file__).with_name("data") / "ieee_features.parquet"

# Attributes on SyntheticDecline that the model may read. The latent cause and
# both potential outcomes are deliberately absent -- they are sealed.
FEATURE_COLUMNS: tuple[str, ...] = (
    "amount_paise",
    "hour_of_day",
    "day_of_month",
    "days_since_payday",
    "attempts_prior_1h",
    "in_outage",
    "amount_z",
    "customer_prior_success",
)


class FeatureSource(Protocol):
    """Supplies base transaction features. Marginals only -- no causal content."""

    def sample(self, n: int, rng: np.random.Generator) -> dict[str, np.ndarray]: ...


class SyntheticFeatureSource:
    """Fallback with hand-specified marginals.

    Amounts are lognormal because real payment amounts are: a heavy right tail
    with most mass low. Hours follow a double-peaked daily shape rather than a
    uniform one, so time-of-day carries the signal it does in reality.
    """

    def sample(self, n: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
        hours = np.arange(24)
        shape = (
            0.4
            + np.exp(-0.5 * ((hours - 12) / 3.0) ** 2)
            + 1.3 * np.exp(-0.5 * ((hours - 20) / 2.5) ** 2)
        )
        return {
            "amount_paise": np.clip(
                rng.lognormal(mean=9.2, sigma=1.1, size=n), 5_000, 5_000_000
            ).astype(np.int64),
            "hour_of_day": rng.choice(hours, size=n, p=shape / shape.sum()),
            "network": rng.choice(
                ["visa", "mastercard", "rupay", "amex"], size=n, p=[0.52, 0.28, 0.17, 0.03]
            ),
            "funding": rng.choice(["debit", "credit"], size=n, p=[0.68, 0.32]),
        }


class IEEECISFeatureSource:
    """Resamples rows from the committed IEEE-CIS distillation.

    Real amount distribution, real time-of-day shape, real network mix. Built by
    scripts/prepare_ieee.py; runtime never touches Kaggle.
    """

    def __init__(self, path: Path | None = None) -> None:
        import pandas as pd

        self._df = pd.read_parquet(path or ARTIFACT)

    @staticmethod
    def available(path: Path | None = None) -> bool:
        return (path or ARTIFACT).exists()

    def sample(self, n: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
        idx = rng.integers(0, len(self._df), size=n)
        rows = self._df.iloc[idx]
        net = rows["card4"].astype(str).to_numpy()
        return {
            "amount_paise": rows["amount_paise"].to_numpy(dtype=np.int64),
            "hour_of_day": rows["hour_of_day"].to_numpy(dtype=np.int64),
            "network": np.where(net == "american express", "amex", net),
            "funding": rows["card6"].astype(str).to_numpy(),
        }


def default_feature_source() -> FeatureSource:
    """Real features when the artifact is present, synthetic otherwise.

    A corrupt or unreadable artifact degrades to synthetic rather than crashing:
    the feature source affects realism, never correctness, so it must not be
    able to take the build down.
    """
    if IEEECISFeatureSource.available():
        try:
            return IEEECISFeatureSource()
        except Exception as exc:  # pragma: no cover - corrupt artifact
            logging.getLogger(__name__).warning(
                "ieee_features.parquet unreadable (%s); falling back to synthetic", exc
            )
    return SyntheticFeatureSource()


__all__ = [
    "ARTIFACT",
    "FEATURE_COLUMNS",
    "FeatureSource",
    "IEEECISFeatureSource",
    "SyntheticFeatureSource",
    "default_feature_source",
]
