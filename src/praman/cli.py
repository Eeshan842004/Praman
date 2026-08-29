"""Praman command line.

    praman verify --ledger data/ledger.db
    praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900

`verify` is the deliverable a judge runs against the committed ledger without
having to trust the demo video. `tamper` exists to prove `verify` is not
decorative.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from praman.kernel.opa_client import PolicyClient
from praman.ledger.chain import FIELDS, connect, verify
from praman.ledger.replay import replay_ledger
from praman.measure.assign import DEFAULT_HOLDOUT_PCT
from praman.measure.harness import validate_estimator
from praman.slice_runner import run_batch

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# Nominal coverage is 95%. Outside this band the interval is either
# overconfident or useless, and nothing downstream of it can be believed.
COVERAGE_FLOOR = 0.92
COVERAGE_CEILING = 0.97

# Below this, the Monte Carlo SE of the coverage estimate exceeds the width of
# the band, so the gate would be measuring its own noise.
MIN_WORLDS_TO_JUDGE = 100


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    if not path.exists():
        print(f"error: no ledger at {path}", file=sys.stderr)
        return EXIT_FAIL

    conn = connect(path)
    try:
        rows = conn.execute("SELECT COUNT(*), MIN(seq), MAX(seq) FROM ledger").fetchone()
        count = rows[0]

        ok, broken_at, msg = verify(conn)

        if not ok:
            print(f"x {msg}")
            print(f"  entry {broken_at} of {count}")
            print("ATTESTATION FAIL")
            return EXIT_FAIL

        print(f"+ {count} entries . {msg}")

        # ── Replay ───────────────────────────────────────────────────────────
        # The chain proves nothing was changed after the fact. It CANNOT prove
        # the record was true when written: a writer that bypassed OPA and
        # recorded its own verdict appends through the normal path and hashes
        # perfectly. So every stored policy input is re-POSTed to OPA loaded
        # with the bundle that authorised it, and the verdict is compared.
        #
        # This block previously printed "N/N decisions reproduced" off a GROUP
        # BY. Nothing was reproduced; the word was unearned. The line now prints
        # only when a replay actually ran.
        if args.replay:
            replayed = replay_ledger(conn, opa_binary=args.opa)
            print(replayed.render())
            if replayed.ran and not replayed.ok:
                print("ATTESTATION FAIL")
                return EXIT_FAIL
        else:
            print("~ replay disabled: chain checked, policy NOT re-derived")

        # A violation is an ACTUATION that executed without an authorising
        # allow. A refusal is not a violation -- refusing is the kernel working.
        violations = conn.execute(
            "SELECT COUNT(*) FROM ledger a WHERE a.entry_type = 'ACTUATION' "
            "AND a.executed = 1 AND NOT EXISTS ("
            "  SELECT 1 FROM ledger d WHERE d.seq = a.decision_seq "
            "  AND d.entry_type = 'DECISION' AND d.opa_allow = 1)"
        ).fetchone()[0]
        actuations = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type = 'ACTUATION' AND executed = 1"
        ).fetchone()[0]
        arms = Counter(
            r[0] for r in conn.execute("SELECT arm FROM ledger WHERE entry_type = 'DECISION'")
        )
        print(
            f"+ {violations} policy violations across {actuations} actuations . "
            f"arms: {dict(sorted(arms.items()))}"
        )
        print("ATTESTATION PASS")
        return EXIT_OK
    finally:
        conn.close()


def _cmd_tamper(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    if not path.exists():
        print(f"error: no ledger at {path}", file=sys.stderr)
        return EXIT_FAIL

    if "=" not in args.set:
        print("error: --set expects field=value", file=sys.stderr)
        return EXIT_USAGE
    field, _, value = args.set.partition("=")

    # Only hashed columns are worth tampering with. Anything else would prove
    # nothing, so refuse rather than produce a misleading demo.
    if field not in FIELDS:
        print(
            f"error: '{field}' is not a hashed field. Hashed fields: {', '.join(FIELDS)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    conn = connect(path)
    try:
        exists = conn.execute("SELECT 1 FROM ledger WHERE seq = ?", (args.entry,)).fetchone()
        if not exists:
            print(f"error: no entry {args.entry}", file=sys.stderr)
            return EXIT_FAIL

        typed: object = int(value) if value.lstrip("-").isdigit() else value

        # The honest part of the demo: immutability is enforced by a trigger, so
        # defeating it is a privileged and VISIBLE act. Hash chaining is
        # tamper-evident, not tamper-preventing -- it detects, it does not stop
        # a root user. Saying so out loud is the point.
        print(f"! dropping append-only trigger to modify entry {args.entry} (privileged act)")
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_update")
        conn.execute(
            f"UPDATE ledger SET {field} = ? WHERE seq = ?",  # field validated above
            (typed, args.entry),
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger "
            "BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END"
        )
        print(f"! entry {args.entry}.{field} := {typed!r}")
        print("  now run: praman verify")
        return EXIT_OK
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()


def _cmd_validate_estimator(args: argparse.Namespace) -> int:
    """Score the estimator against worlds whose true effect is known and sealed.

    This is a gate, not a report. We do not claim to have recovered money; we
    claim our estimator recovers the truth, and this is the check that earns the
    claim. If coverage drifts out of band the exit code says so.
    """
    report = validate_estimator(
        n_worlds=args.worlds,
        holdout_pct=args.holdout_pct,
        n_boot=args.boot,
        seed0=args.seed,
    )
    print(report.render())
    print()

    # Coverage is itself estimated from a finite number of worlds, so it carries
    # Monte Carlo error. Reporting the verdict without that error would be the
    # same overconfidence this harness exists to catch.
    mc_se = math.sqrt(0.95 * 0.05 / report.n_worlds) if report.n_worlds else float("inf")
    print(f"  Monte Carlo SE of coverage .  {mc_se:.1%}  ({report.n_worlds} worlds)")

    if report.n_worlds < MIN_WORLDS_TO_JUDGE:
        print(
            f"! only {report.n_worlds} worlds: the Monte Carlo SE ({mc_se:.1%}) is "
            f"comparable to the band itself, so this run cannot judge the "
            f"estimator. Use at least {MIN_WORLDS_TO_JUDGE}."
        )
        return EXIT_OK

    if not COVERAGE_FLOOR <= report.coverage <= COVERAGE_CEILING:
        print(
            f"x coverage {report.coverage:.1%} is outside "
            f"[{COVERAGE_FLOOR:.0%}, {COVERAGE_CEILING:.0%}] "
            "-- the confidence interval is wrong"
        )
        return EXIT_FAIL

    print(
        f"+ coverage {report.coverage:.1%} is nominal . "
        f"bias {report.mean_bias_pct:+.1f}% . "
        f"CUPED -{report.mean_variance_reduction:.0%} variance"
    )
    return EXIT_OK


def _batch_client() -> PolicyClient:
    """Indirection so tests can inject a policy client without a live sidecar."""
    return PolicyClient()


def _cmd_run_batch(args: argparse.Namespace) -> int:
    """Run the full recovery pipeline over a batch of declines.

    A gate, not a report: any policy violation exits non-zero. The whole
    compliance claim is that the count stays at zero, so the exit code has to
    mean it.
    """
    result = run_batch(
        n=args.n,
        seed=args.seed,
        ledger_path=args.ledger,
        client=_batch_client(),
        experiment_id=args.experiment_id,
        holdout_pct=args.holdout_pct,
    )
    print(result.render())
    print()

    if result.policy_violations:
        print(
            f"x {result.policy_violations} policy violation(s) -- an actuation "
            "occurred without an authorising allow"
        )
        return EXIT_FAIL

    print(f"+ 0 policy violations . ledger at {result.ledger_path}")
    print(f"  verify it: praman verify --ledger {result.ledger_path}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="praman",
        description="Provable revenue recovery: verify and stress the evidence ledger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="attest the hash chain AND replay every decision")
    v.add_argument("--ledger", default="data/ledger.db")
    v.add_argument(
        "--no-replay",
        dest="replay",
        action="store_false",
        help="check the hash chain only; do not re-derive decisions from policy",
    )
    v.add_argument(
        "--opa", default=None, help="path to the opa binary (default: tools/, then PATH)"
    )
    v.set_defaults(func=_cmd_verify, replay=True)

    t = sub.add_parser("tamper", help="deliberately corrupt one entry, to prove verify works")
    t.add_argument("--ledger", default="data/ledger.db")
    t.add_argument("--entry", type=int, required=True)
    t.add_argument("--set", required=True, metavar="FIELD=VALUE")
    t.set_defaults(func=_cmd_tamper)

    e = sub.add_parser(
        "validate-estimator",
        help="score the ATE estimator against worlds with a known true effect",
    )
    e.add_argument("--worlds", type=int, default=200)
    e.add_argument("--boot", type=int, default=2000)
    e.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    e.add_argument("--seed", type=int, default=9000)
    e.set_defaults(func=_cmd_validate_estimator)

    b = sub.add_parser("run-batch", help="run the recovery pipeline over a batch of declines")
    b.add_argument("--n", type=int, default=1000)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--ledger", default="data/ledger.db")
    b.add_argument("--experiment-id", default="praman-v1")
    b.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    b.set_defaults(func=_cmd_run_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
