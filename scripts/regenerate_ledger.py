"""Regenerate the evidence ledger.

Two modes, and the difference is size, not honesty:

    demo   ~3 MB, 1,200 decisions across BOTH pinned bundles. This is the file
           committed to the repo so a judge can clone and attest in under a
           minute without generating anything.
    full   the complete run behind the reported figures -- the pre-unfreeze
           span, the powered batch and the underpowered batch. ~23 MB, which is
           more than a git clone should carry for a demo.

Both span two bundle revisions on purpose. A single-revision ledger can only
show that we can audit a policy; two revisions show we can audit a policy
CHANGE, and each span replays against the bundle that actually authorised it.

The first span is written against bundle 4ca4787c, in which human escalation
could still be denied, and the rest against bd45b0c7, in which it cannot. The
tier distributions therefore differ across the boundary, visibly, which is the
point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from praman.kernel.opa_client import PolicyClient  # noqa: E402
from praman.ledger.replay import BundleServer, find_opa  # noqa: E402
from praman.slice_runner import run_batch  # noqa: E402

OLD_BUNDLE = REPO / "dist" / "bundle-4ca4787c0a1eea75.tar.gz"
NEW_BUNDLE = REPO / "dist" / "bundle-bd45b0c7e5ce66a3.tar.gz"

# (bundle, n, seed, experiment_id)
PLANS = {
    "demo": [
        (OLD_BUNDLE, 400, 7, "praman-v0-pre-unfreeze"),
        (NEW_BUNDLE, 800, 42, "praman-demo"),
    ],
    "full": [
        (OLD_BUNDLE, 1200, 7, "praman-v0-pre-unfreeze"),
        (NEW_BUNDLE, 5000, 42, "praman-powered"),
        (NEW_BUNDLE, 3000, 42, "praman-underpowered"),
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(PLANS), default="demo")
    parser.add_argument("--out", default=str(REPO / "data" / "ledger.db"))
    args = parser.parse_args()

    opa = find_opa()
    if opa is None:
        print("error: no opa binary. Run scripts/bootstrap.ps1 first.", file=sys.stderr)
        return 1

    out = Path(args.out)
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(out) + suffix)
        if target.exists():
            target.unlink()

    for bundle, n, seed, experiment in PLANS[args.mode]:
        if not bundle.exists():
            print(f"error: {bundle} is not committed", file=sys.stderr)
            return 1
        # One OPA per bundle: every decision in this span is authorised by, and
        # will later replay against, that exact revision.
        with BundleServer(bundle, opa) as server:
            result = run_batch(
                n=n,
                seed=seed,
                ledger_path=out,
                client=PolicyClient(base_url=server.url),
                experiment_id=experiment,
            )
        print(
            f"  {bundle.name}  {experiment:<24} "
            f"{result.n_declines:>5} declines  {result.n_actuated:>5} actuations  "
            f"tiers {dict(sorted(result.tier_counts.items()))}"
        )

    # Fold the write-ahead log into the main file. Committing ledger.db while
    # its recent pages still live in ledger.db-wal would commit a truncated
    # chain that fails attestation on a fresh clone.
    import sqlite3

    conn = sqlite3.connect(str(out))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_mb = out.stat().st_size / 1_048_576
    print(f"\n  wrote {out}  ({size_mb:.1f} MB)")
    print(f"  attest it: uv run praman verify --ledger {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
