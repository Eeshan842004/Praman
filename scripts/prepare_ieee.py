"""One-time: distil IEEE-CIS into a small committed feature artifact.

Runtime never touches Kaggle. This script reads the downloaded competition CSV
once, keeps only the columns the feature adapter resamples, and writes a compact
parquet that ships in the repo. That keeps the feature MARGINALS real while the
causal label layer stays synthetic -- which is the whole point: published work
shows synthetic tabular generators break velocity and temporal structure, so we
resample real feature rows rather than inventing them.

    uv run python scripts/prepare_ieee.py

Input : data/raw/train_transaction.csv.zip   (Kaggle, not committed)
Output: src/praman/sim/data/ieee_features.parquet  (committed, ~1 MB)
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC_ZIP = ROOT / "data" / "raw" / "train_transaction.csv.zip"
OUT = ROOT / "src" / "praman" / "sim" / "data" / "ieee_features.parquet"

# Only what the adapter resamples. Everything else is noise we would ship for
# nothing.
COLUMNS = [
    "TransactionAmt",  # real amount distribution: heavy right tail, round-number spikes
    "ProductCD",
    "card4",  # network: visa / mastercard / discover / amex
    "card6",  # funding: debit / credit
    "P_emaildomain",
    "TransactionDT",  # seconds from an arbitrary origin; gives real time-of-day shape
]

N_SAMPLE = 60_000
SEED = 42


def main() -> int:
    if not SRC_ZIP.exists():
        print(f"missing {SRC_ZIP}", file=sys.stderr)
        print(
            "run: uv run kaggle competitions download -c ieee-fraud-detection "
            "-f train_transaction.csv -p data/raw",
            file=sys.stderr,
        )
        return 1

    with zipfile.ZipFile(SRC_ZIP) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            df = pd.read_csv(fh, usecols=COLUMNS)

    print(f"read {len(df):,} rows from {name}")

    rng = np.random.default_rng(SEED)
    idx = rng.choice(len(df), size=min(N_SAMPLE, len(df)), replace=False)
    sample = df.iloc[np.sort(idx)].reset_index(drop=True)

    # Derive the time-of-day shape now so the adapter stays trivial.
    sample["hour_of_day"] = ((sample["TransactionDT"] // 3600) % 24).astype("int16")
    sample["amount_paise"] = (sample["TransactionAmt"] * 100).round().astype("int64")
    sample = sample.drop(columns=["TransactionDT", "TransactionAmt"])

    for col in ("ProductCD", "card4", "card6", "P_emaildomain"):
        sample[col] = sample[col].fillna("unknown").astype("category")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sample.to_parquet(OUT, compression="zstd", index=False)

    size_mb = OUT.stat().st_size / 1e6
    print(f"wrote {len(sample):,} rows -> {OUT.relative_to(ROOT)} ({size_mb:.2f} MB)")
    print(
        f"  amount_paise  p50={sample.amount_paise.median():,.0f} "
        f"p99={sample.amount_paise.quantile(0.99):,.0f}"
    )
    print(f"  hour_of_day   mode={sample.hour_of_day.mode().iloc[0]}")
    print(f"  networks      {dict(sample.card4.value_counts().head(4))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
