"""Features the model is allowed to see.

The list is explicit rather than "every column except the sealed ones". An
allowlist fails safe when a new field is added to `SyntheticDecline`: the field
is simply not used until someone deliberately adds it here. A denylist fails
open, and the failure is silent -- a leaked outcome field would raise the ICR
above 1.0 and look like success.

Three things are sealed and must never appear: `latent_cause` is the answer, and
`y0_recovered` / `y1_recovered` are the potential outcomes the estimator exists
to infer. `cuped_covariate` is excluded too. It is not an outcome, but it is
built from the customer's reliability -- the same quantity that drives recovery
-- and it is the estimator's variance-reduction covariate. Letting the
attribution model consume it would tangle the inference and the measurement,
which are deliberately kept apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

# Continuous and count features.
NUMERIC: tuple[str, ...] = (
    "amount_paise",
    "hour_of_day",
    "day_of_month",
    "days_since_payday",
    "attempts_prior_1h",
    "in_outage",
    "amount_z",
    "customer_prior_success",
)

# Categorical. `symbol` is the decline code itself and carries most of the
# signal; it MUST be categorical, because '05' < '51' is an ordering the payment
# networks never intended and a tree would happily split on it.
CATEGORICAL: tuple[str, ...] = (
    "symbol",
    "rail",
    "network",
    "funding",
    "cvv_result",
    "expiry_valid",
)

FEATURE_SPEC: tuple[str, ...] = NUMERIC + CATEGORICAL

# Named so the leakage test can assert against something concrete rather than a
# hand-maintained literal in the test file.
SEALED: tuple[str, ...] = (
    "latent_cause",
    "y0_recovered",
    "y1_recovered",
    "cuped_covariate",
)


def to_frame(declines: Sequence[Any]) -> pd.DataFrame:
    """Build the model's input frame from decline records.

    Accepts anything with the attributes in FEATURE_SPEC, so a `SyntheticDecline`
    from the simulator and a normalised live decline go through the same path.
    """
    rows = [{name: getattr(d, name, None) for name in FEATURE_SPEC} for d in declines]
    frame = pd.DataFrame(rows, columns=list(FEATURE_SPEC))

    for name in CATEGORICAL:
        # Strings, so None and False do not collapse into the same category and
        # so LightGBM sees a stable set of levels across train and predict.
        frame[name] = frame[name].astype("string").fillna("__missing__").astype("category")

    for name in NUMERIC:
        frame[name] = pd.to_numeric(frame[name], errors="coerce").astype("float64")

    return frame


def align_categories(frame: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    """Force `frame`'s categorical levels to match those seen in training.

    Without this, a batch that happens to contain no `upi_autopay` row encodes
    `rail` with different integer codes than training did, and the model reads
    the wrong feature entirely -- silently, with no error and plausible output.
    """
    out = frame.copy()
    for name in CATEGORICAL:
        out[name] = out[name].cat.set_categories(reference[name].cat.categories)
    return out


__all__ = ["CATEGORICAL", "FEATURE_SPEC", "NUMERIC", "SEALED", "align_categories", "to_frame"]
