"""Replay attestation: re-evaluate every recorded decision against the bundle
that authorised it.

The hash chain and this module prove two DIFFERENT things, and the difference is
the whole reason this file exists.

    chain  -- nothing was changed after the fact
    replay -- what was written was true when it was written

A chain cannot detect a writer that bypassed OPA and recorded its own verdict:
the row is appended through the normal path, so the hash is perfect and the
attestation passes. `policy_input_json` exists precisely so that claim can be
re-derived rather than trusted -- and storing it is only half the mechanism. The
other half is here.

Replay catches three things a hash chain structurally cannot:

    a forged verdict   an allow recorded for an input the policy denies
    policy drift       the bundle changed but the revision did not (S4)
    a mis-pinned row   the bundle does not self-report the pinned revision

Mechanism: each distinct `bundle_revision` in the ledger names a committed
`dist/bundle-<revision>.tar.gz`. We start OPA against that bundle alone, on an
ephemeral port, and re-POST every stored input through the SAME `PolicyClient`
production uses. Nothing about the evaluation path is special-cased for replay,
because a replay path that differs from the live path proves nothing about the
live path.

The bundle is self-describing: `policy/revision/data.json` travels inside the
tarball, so the replayed decision echoes its own revision back. If a bundle were
swapped, its echo would not match the ledger and every row under it diverges.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from praman.config import REPO_ROOT
from praman.kernel.opa_client import PolicyClient

BUNDLE_DIR = REPO_ROOT / "dist"

# Long enough for a cold process start on a loaded laptop, short enough that a
# genuinely broken bundle fails the command rather than hanging a demo.
_STARTUP_TIMEOUT_S = 25.0
_POLL_INTERVAL_S = 0.05


def find_opa(explicit: str | Path | None = None) -> Path | None:
    """Locate the OPA binary: an explicit path, the vendored one, then PATH."""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        # A shell that resolved `tools/opa` (Git Bash appends .exe silently)
        # hands us a path Python cannot see. Without this the verify script
        # skips replay on Windows and degrades to a chain-only check.
        with_exe = p.with_suffix(".exe")
        return with_exe if with_exe.exists() else None

    vendored = REPO_ROOT / "tools" / ("opa.exe" if os.name == "nt" else "opa")
    if vendored.exists():
        return vendored

    from shutil import which

    found = which("opa")
    return Path(found) if found else None


def bundle_for(revision: str, bundle_dir: Path | None = None) -> Path | None:
    """The committed bundle a revision names. `dist/` is evidence, not build output."""
    candidate = (bundle_dir or BUNDLE_DIR) / f"bundle-{revision}.tar.gz"
    return candidate if candidate.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class BundleServer:
    """An OPA process serving exactly one pinned bundle, on an ephemeral port.

    One process per revision rather than one per decision: process startup
    dominates evaluation by three orders of magnitude, and a ledger spanning two
    bundles should cost two starts, not fourteen thousand.
    """

    __slots__ = ("_bundle", "_opa", "_port", "_proc")

    def __init__(self, bundle: Path, opa: Path) -> None:
        self._bundle = Path(bundle)
        self._opa = Path(opa)
        self._port = _free_port()
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self) -> BundleServer:
        self._proc = subprocess.Popen(  # noqa: S603 - paths are ours, not user input
            [
                str(self._opa),
                "run",
                "--server",
                "--addr",
                f"127.0.0.1:{self._port}",
                "--bundle",
                str(self._bundle),
                "--log-level",
                "error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._await_ready()
        return self

    def _await_ready(self) -> None:
        """Wait for bundle ACTIVATION, not merely for the port to open.

        `/health` alone returns 200 as soon as the server is listening, which can
        be before the bundle is compiled and activated. Querying then would hit
        an undefined path -- a 200 with an empty body -- and the client would
        correctly fail closed, turning a startup race into a wall of phantom
        divergences. `?bundles=true` is the flag that makes readiness mean what
        we need it to mean.
        """
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"opa exited with code {self._proc.returncode} while loading "
                    f"{self._bundle.name}"
                )
            try:
                r = httpx.get(f"{self.url}/health?bundles=true", timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception as exc:  # not up yet
                last = exc
            time.sleep(_POLL_INTERVAL_S)
        raise TimeoutError(f"opa did not become ready for {self._bundle.name}: {last}")

    def __exit__(self, *exc: object) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None


@dataclass(frozen=True, slots=True)
class Divergence:
    """One recorded decision the pinned policy does not reproduce."""

    seq: int
    revision: str
    attribute: str  # "allow" | "deny_reasons" | "bundle_revision"
    recorded: str
    replayed: str

    def render(self) -> str:
        return (
            f"    entry {self.seq} [{self.revision}] {self.attribute}: "
            f"recorded {self.recorded} -> replayed {self.replayed}"
        )


@dataclass(slots=True)
class ReplayReport:
    total: int = 0
    reproduced: int = 0
    unreplayable: int = 0
    spans: list[tuple[str, int, int, int]] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def diverged(self) -> int:
        return len({d.seq for d in self.divergences})

    @property
    def ran(self) -> bool:
        """Did any decision actually get re-evaluated?"""
        return bool(self.spans)

    @property
    def ok(self) -> bool:
        """Every decision reproduced, or this is not an attestation.

        Three ways to fail, and all three must count:
          * a divergence -- the pinned policy does not produce the record;
          * a decision that stored no input -- unproven, not proven;
          * a decision under a bundle that is not committed -- never replayed.

        The third is the subtle one. Those rows are in `total` but in neither
        `reproduced` nor `unreplayable`, so testing only the first two would
        report a partial replay with a leading "+" and pass the attestation
        while some decisions were never checked at all. Requiring
        reproduced == total closes it by construction.
        """
        return not self.divergences and self.unreplayable == 0 and self.reproduced == self.total

    def render(self) -> str:
        if not self.ran:
            reasons = "; ".join(f"{r}: {why}" for r, why in self.skipped.items())
            return f"~ replay skipped -- {reasons or 'no decisions to replay'}"

        lines = [
            f"{'+' if self.ok else 'x'} {self.reproduced}/{self.total} decisions "
            f"reproduced against {len(self.spans)} pinned bundle(s)"
        ]
        for rev, n, lo, hi in self.spans:
            # "decision entries", because the range is MIN/MAX over DECISION
            # rows: the gap between consecutive spans is the actuation and
            # outcome rows of the earlier span's last decision, which carry no
            # revision. Labelling it "entries" invites a question about the
            # missing numbers that the output cannot answer.
            lines.append(f"    bundle {rev} : decision entries {lo}-{hi}  ({n} decisions)")
        if self.unreplayable:
            lines.append(
                f"  x {self.unreplayable} decision(s) stored no policy input and cannot be replayed"
            )
        if self.divergences:
            lines.append(f"  x {self.diverged} decision(s) diverged from the pinned policy:")
            lines.extend(d.render() for d in self.divergences[:10])
            if len(self.divergences) > 10:
                lines.append(f"    ... and {len(self.divergences) - 10} more")
        for rev, why in self.skipped.items():
            lines.append(f"  ~ bundle {rev} not replayed: {why}")
        return "\n".join(lines)


_DECISIONS = """
SELECT seq, bundle_revision, policy_input_json, opa_allow, deny_reasons
FROM   ledger
WHERE  entry_type = 'DECISION'
ORDER  BY seq
"""


def _compare(
    seq: int,
    revision: str,
    recorded_allow: int,
    recorded_deny: str,
    replayed: Any,
) -> list[Divergence]:
    out: list[Divergence] = []

    if bool(recorded_allow) != replayed.allow:
        out.append(
            Divergence(seq, revision, "allow", str(bool(recorded_allow)), str(replayed.allow))
        )

    try:
        stored_reasons = sorted(json.loads(recorded_deny or "[]"))
    except json.JSONDecodeError:
        stored_reasons = ["<unparseable>"]
    if stored_reasons != list(replayed.deny_reasons):
        out.append(
            Divergence(
                seq, revision, "deny_reasons", str(stored_reasons), str(replayed.deny_reasons)
            )
        )

    # The bundle carries its own revision, so this catches a swapped or
    # re-cut bundle even when the verdict happens to match.
    if replayed.bundle_revision != revision:
        out.append(Divergence(seq, revision, "bundle_revision", revision, replayed.bundle_revision))

    return out


def replay_ledger(
    conn: sqlite3.Connection,
    bundle_dir: Path | None = None,
    opa_binary: str | Path | None = None,
) -> ReplayReport:
    """Re-evaluate every stored decision against its own pinned bundle."""
    report = ReplayReport()

    rows = conn.execute(_DECISIONS).fetchall()
    report.total = len(rows)
    if not rows:
        return report

    opa = find_opa(opa_binary)
    if opa is None:
        report.skipped["*"] = "no opa binary (looked in tools/ and PATH)"
        return report

    # Group by revision, preserving ledger order so the printed spans read as a
    # timeline of policy changes rather than an arbitrary set.
    by_revision: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        by_revision.setdefault(row[1], []).append(row)

    for revision, group in by_revision.items():
        seqs = [r[0] for r in group]
        bundle = bundle_for(revision, bundle_dir)
        if bundle is None:
            report.skipped[revision] = f"dist/bundle-{revision}.tar.gz is not committed"
            continue

        with BundleServer(bundle, opa) as server, PolicyClient(base_url=server.url) as client:
            for seq, _rev, raw_input, allow, deny in group:
                try:
                    stored_input = json.loads(raw_input) if raw_input else {}
                except json.JSONDecodeError:
                    stored_input = {}

                # An empty input is not a pass. The decision was recorded without
                # the evidence needed to re-derive it, so it stays unproven.
                if not stored_input:
                    report.unreplayable += 1
                    continue

                divergences = _compare(seq, revision, allow, deny, client.evaluate(stored_input))
                if divergences:
                    report.divergences.extend(divergences)
                else:
                    report.reproduced += 1

        report.spans.append((revision, len(group), min(seqs), max(seqs)))

    return report


__all__ = [
    "BUNDLE_DIR",
    "BundleServer",
    "Divergence",
    "ReplayReport",
    "bundle_for",
    "find_opa",
    "replay_ledger",
]
