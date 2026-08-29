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

from praman.ingest.store import connect_ingest, pending
from praman.ingest.worker import process_pending
from praman.kernel.opa_client import PolicyClient
from praman.ledger.chain import FIELDS, connect, verify
from praman.ledger.replay import replay_ledger
from praman.measure.assign import DEFAULT_HOLDOUT_PCT
from praman.measure.harness import payments_world, toy_world, validate_estimator
from praman.measure.power import DEFAULT_GRID, PowerCurve, power_curve
from praman.measure.report import build_report
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
    # Default to the payments simulator, not the toy world. The toy world
    # proved the estimator's CONTRACT before the simulator existed and is still
    # useful for that -- but the naive estimator's bias is scale-dependent, so
    # quoting a toy-world figure as the headline would be quoting a number about
    # a domain-free generator. The headline must be measured where it is used.
    factory = toy_world if args.world == "toy" else (lambda s: payments_world(s, n=args.world_n))
    report = validate_estimator(
        n_worlds=args.worlds,
        holdout_pct=args.holdout_pct,
        n_boot=args.boot,
        seed0=args.seed,
        world_factory=factory,
    )
    scope = "" if args.world == "toy" else f" (n={args.world_n} per world)"
    print(f"world: {args.world}{scope}")
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


def _cmd_power(args: argparse.Namespace) -> int:
    """Compute the minimum detectable effect, and let the curve choose n.

    This runs BEFORE a headline batch, not after. An interval that straddles
    zero is only a finding if you did not already know the design could not
    resolve the effect; otherwise it is an arithmetic result you should have
    computed in advance. This is that computation.
    """
    if args.load:
        curve = PowerCurve.from_json(Path(args.load).read_text(encoding="utf-8"))
        print(f"loaded measured curve from {args.load}")
        return _render_power(curve)

    grid = tuple(int(x) for x in args.grid.split(",")) if args.grid else DEFAULT_GRID
    curve: PowerCurve = power_curve(
        grid=grid,
        # 0 means auto: allocate replicates by observation budget so every grid
        # point is measured to roughly equal precision. A fixed small count made
        # the curve report its own sampling noise.
        seeds_per_point=args.seeds or None,
        client_factory=_batch_client,
        holdout_pct=args.holdout_pct,
    )
    if args.save:
        Path(args.save).write_text(curve.to_json(), encoding="utf-8")
        print(f"saved measured curve to {args.save}")
    return _render_power(curve)


def _render_power(curve: PowerCurve) -> int:
    print(curve.render())
    print()

    effect = curve.true_effect_paise

    # Quote the MEASURED point at n=3,000, never the fitted one. The fit exists
    # to extrapolate beyond the grid; using it where a measurement exists would
    # let a bad fit quietly rewrite the headline claim.
    measured = next((p for p in curve.points if p.n == 3000), None)
    at_3000 = measured.mde_paise if measured else curve.mde_at(3000)
    source = "measured" if measured else "fitted"

    print(f"  (a) at n=3,000 the MDE at 80% power is Rs {at_3000 / 100:,.2f} ({source}).")
    print(
        f"      The true ITT effect is Rs {effect / 100:,.2f}, which is "
        f"{effect / at_3000:.2f}x the MDE."
    )
    if effect < at_3000:
        print("      The effect sits BELOW the MDE: that batch could not have resolved")
        print("      it at 80% power. The null was arithmetic, not evidence -- and it")
        print("      is computed here rather than discovered in a straddling interval.")
    else:
        print("      The effect sits above the MDE at this n.")
    print()

    # The two rules are printed side by side on purpose. They disagree, and the
    # disagreement is a finding rather than a defect to hide: the 1/sqrt(n) law
    # is asymptotically correct but converges slowly on a heavy-tailed outcome,
    # so the fit under-describes the variance at these sample sizes.
    by_grid = curve.required_n_measured(effect)
    by_fit = curve.required_n_fitted(effect)
    required = curve.required_n(effect)

    print(f"  (b) n from the measured grid (monotone) .......  {by_grid:,}")
    print(f"      n from the fitted 1/sqrt(n) law ..........  {by_fit:,}")
    if measured and measured.mde_paise > 0:
        residual = abs(curve.mde_at(3000) - measured.mde_paise) / measured.mde_paise
        if residual > 0.15:
            print()
            print(f"      The fit misses the MEASURED MDE at n=3,000 by {residual:.0%}, so it")
            print("      is reported as a cross-check and not used to choose n. Payment")
            print("      outcomes are amount x Bernoulli over a lognormal amount, so the")
            print("      thin holdout arm converges slowly and the fit runs optimistic.")
    print()

    if not required:
        print("  x no batch size on this grid resolves the effect. Extend the grid.")
        return EXIT_FAIL

    print(
        f"      RECOMMENDED n ............................  {required:,}"
        "   (the more conservative rule)"
    )
    point = next((p for p in curve.points if p.n == required), None)
    if point and point.se_paise > 0:
        print(f"      measured MDE at that n ...................  Rs {point.mde_paise / 100:,.2f}")
        print(
            f"      effect / SE ..............................  {effect / point.se_paise:.2f} sigma"
        )
    print()
    print(f"      praman run-batch --n {required} --experiment-id praman-powered")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    """Print the result in three tiers, headline first.

    A gate as well as a report: if the estimator's coverage is not nominal, the
    tiers below it are not entitled to be believed, so the command says so and
    exits non-zero rather than printing them as though they stood on their own.
    """
    report = build_report(
        powered_n=args.powered_n,
        illustrative_n=args.illustrative_n,
        n_worlds=args.worlds,
        world_n=args.world_n,
        n_boot=args.boot,
        holdout_pct=args.holdout_pct,
        ledger_dir=Path(args.ledger_dir),
        client_factory=_batch_client,
    )
    print(report.render())
    print()

    primary = report.primary
    if primary is not None and not (COVERAGE_FLOOR <= primary.coverage <= COVERAGE_CEILING):
        print(
            f"x PRIMARY coverage {primary.coverage:.1%} is outside "
            f"[{COVERAGE_FLOOR:.0%}, {COVERAGE_CEILING:.0%}] -- the interval is wrong, "
            "so nothing below it can be believed"
        )
        return EXIT_FAIL

    violations = sum(
        run.policy_violations for run in (report.secondary, report.illustrative) if run
    )
    if violations:
        print(f"x {violations} policy violation(s)")
        return EXIT_FAIL

    print("+ coverage nominal . 0 policy violations")
    return EXIT_OK


def _cmd_process_webhooks(args: argparse.Namespace) -> int:
    """Drain accepted webhook deliveries into ledger decisions.

    Separate from the endpoint on purpose. The request path acknowledges and
    stops; everything that thinks runs here, because a slow acknowledgement
    becomes a Razorpay redelivery and a redelivery that reached the attempt
    counter is a regulatory problem, not a latency one (S2).
    """
    conn = connect_ingest(args.ingest)
    try:
        queued = len(pending(conn))
        if not queued:
            print("+ nothing pending")
            return EXIT_OK
        seqs = process_pending(
            conn,
            args.ledger,
            client=_batch_client(),
            experiment_id=args.experiment_id,
            holdout_pct=args.holdout_pct,
        )
    finally:
        conn.close()

    print(f"+ {len(seqs)} of {queued} deliveries became ledger decisions")
    if seqs:
        print(f"  entries {min(seqs)}-{max(seqs)} . verify: praman verify --ledger {args.ledger}")
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
    e.add_argument(
        "--world",
        choices=("payments", "toy"),
        default="payments",
        help="which generator to score against (default: the payments simulator)",
    )
    e.add_argument("--world-n", type=int, default=2000, help="declines per simulated world")
    e.set_defaults(func=_cmd_validate_estimator)

    b = sub.add_parser("run-batch", help="run the recovery pipeline over a batch of declines")
    b.add_argument("--n", type=int, default=1000)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--ledger", default="data/ledger.db")
    b.add_argument("--experiment-id", default="praman-v1")
    b.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    b.set_defaults(func=_cmd_run_batch)

    r = sub.add_parser(
        "report",
        help="the reported result in three tiers: primary, secondary, illustrative",
    )
    r.add_argument("--powered-n", type=int, required=True)
    r.add_argument("--illustrative-n", type=int, default=3000)
    r.add_argument("--worlds", type=int, default=200)
    r.add_argument("--world-n", type=int, default=2000)
    r.add_argument("--boot", type=int, default=1500)
    r.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    r.add_argument("--ledger-dir", default="data")
    r.set_defaults(func=_cmd_report)

    w = sub.add_parser(
        "process-webhooks",
        help="turn accepted webhook deliveries into ledger decisions",
    )
    w.add_argument("--ingest", default="data/ingest.db")
    w.add_argument("--ledger", default="data/ledger.db")
    w.add_argument("--experiment-id", default="praman-v1")
    w.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    w.set_defaults(func=_cmd_process_webhooks)

    p = sub.add_parser(
        "power",
        help="minimum detectable effect and the MDE-vs-n curve that picks the batch size",
    )
    p.add_argument("--grid", default=None, help="comma-separated batch sizes")
    p.add_argument("--save", default=None, help="write the measured curve to this JSON file")
    p.add_argument("--load", default=None, help="render a previously measured curve")
    p.add_argument(
        "--seeds",
        type=int,
        default=0,
        help="runs per grid point (0 = auto, allocated by observation budget)",
    )
    p.add_argument("--holdout-pct", type=int, default=DEFAULT_HOLDOUT_PCT)
    p.set_defaults(func=_cmd_power)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
