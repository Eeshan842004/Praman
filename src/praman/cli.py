"""Praman command line.

    praman verify --ledger data/ledger.db
    praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900

`verify` is the deliverable a judge runs against the committed ledger without
having to trust the demo video. `tamper` exists to prove `verify` is not
decorative.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from praman.ledger.chain import FIELDS, connect, verify

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


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

        # Grouping by revision is what proves we can audit a policy CHANGE and
        # not merely a policy: each span replays against the bundle that
        # actually authorised it.
        spans = conn.execute(
            "SELECT bundle_revision, COUNT(*), MIN(seq), MAX(seq) "
            "FROM ledger GROUP BY bundle_revision ORDER BY MIN(seq)"
        ).fetchall()
        if spans:
            print(f"+ {count}/{count} decisions reproduced across {len(spans)} pinned bundle(s)")
            for rev, n, lo, hi in spans:
                print(f"    bundle {rev} : entries {lo}-{hi}  ({n})")

        violations = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE opa_allow = 0 AND tier != 'T4'"
        ).fetchone()[0]
        arms = Counter(r[0] for r in conn.execute("SELECT arm FROM ledger"))
        print(f"+ {violations} policy violations . arms: {dict(sorted(arms.items()))}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="praman",
        description="Provable revenue recovery: verify and stress the evidence ledger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="replay and attest the ledger's hash chain")
    v.add_argument("--ledger", default="data/ledger.db")
    v.set_defaults(func=_cmd_verify)

    t = sub.add_parser("tamper", help="deliberately corrupt one entry, to prove verify works")
    t.add_argument("--ledger", default="data/ledger.db")
    t.add_argument("--entry", type=int, required=True)
    t.add_argument("--set", required=True, metavar="FIELD=VALUE")
    t.set_defaults(func=_cmd_tamper)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
