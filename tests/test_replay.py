"""Replay attestation -- Demo Beat 5.

The hash chain and replay defend against DIFFERENT attackers, and the tests are
split along that line:

    chain  -- someone edited the ledger after the fact
    replay -- someone wrote a verdict OPA never gave

The second attacker is the one the chain cannot see. They hold the append path,
so their row hashes perfectly and `verify` passes. The only defence is storing
the policy input and re-deriving the verdict from the bundle that authorised it.

Every test here spawns its own OPA against the committed bundle, so nothing
depends on a sidecar already running -- but they do need the binary, and they
skip rather than silently pass when it is absent. A replay test that passes
without replaying is worse than no test.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from praman.kernel.ladder import DeclineContext, build_policy_input
from praman.kernel.opa_client import PolicyClient
from praman.ledger.canonical import GENESIS
from praman.ledger.chain import FIELDS, connect, entry_hash, verify
from praman.ledger.records import DecisionRecord
from praman.ledger.replay import BUNDLE_DIR, BundleServer, bundle_for, find_opa, replay_ledger
from praman.slice_runner import run_batch


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def opa_binary():
    binary = find_opa()
    if binary is None:
        pytest.skip("no opa binary in tools/ or on PATH")
    return binary


@pytest.fixture(scope="module")
def pinned():
    """The committed bundle and the revision it is pinned to.

    `dist/` is evidence, not build output -- the revision is read from the
    filename and must survive the round trip through OPA.
    """
    bundles = sorted(BUNDLE_DIR.glob("bundle-*.tar.gz"))
    if not bundles:
        pytest.skip("no committed bundle in dist/")
    newest = bundles[-1]
    return newest.name.removeprefix("bundle-").removesuffix(".tar.gz"), newest


@pytest.fixture(scope="module")
def live_ledger(tmp_path_factory, pinned, opa_binary):
    """A ledger written against the real OPA and the real bundle.

    Built through `run_batch`, not hand-assembled, so what gets replayed is what
    the production writer actually produces.
    """
    _revision, bundle = pinned
    path = tmp_path_factory.mktemp("replay") / "ledger.db"
    with BundleServer(bundle, opa_binary) as server:
        run_batch(
            n=150,
            seed=5,
            ledger_path=path,
            client=PolicyClient(base_url=server.url),
            experiment_id="replay-test",
        )
    return path


def _rechain(path, from_seq: int = 1) -> None:
    """Recompute every hash so the chain is valid again after a mutation.

    This is the privileged attacker the chain alone cannot stop: they can drop
    the trigger, rewrite a row, and re-derive the chain to hide it. Replay is
    what still catches them, and simulating the full attack is the only way to
    prove that.
    """
    conn = connect(path)
    try:
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_update")
        rows = conn.execute(f"SELECT seq,{','.join(FIELDS)} FROM ledger ORDER BY seq").fetchall()
        prev = GENESIS
        for seq, *values in rows:
            if seq >= from_seq:
                h = entry_hash(prev, dict(zip(FIELDS, values, strict=True)))
                conn.execute(
                    "UPDATE ledger SET prev_hash = ?, entry_hash = ? WHERE seq = ?", (prev, h, seq)
                )
                prev = h
            else:
                prev = conn.execute(
                    "SELECT entry_hash FROM ledger WHERE seq = ?", (seq,)
                ).fetchone()[0]
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger "
            "BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END"
        )
    finally:
        conn.close()


def _first_decision(conn: sqlite3.Connection) -> tuple[int, dict]:
    seq, raw = conn.execute(
        "SELECT seq, policy_input_json FROM ledger WHERE entry_type = 'DECISION' "
        "AND policy_input_json NOT IN ('{}', '') ORDER BY seq LIMIT 1"
    ).fetchone()
    return seq, json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# The bundle is self-describing
# ─────────────────────────────────────────────────────────────────────────────
def test_the_bundle_reports_the_revision_its_filename_claims(pinned, opa_binary):
    """Law #6 in both directions.

    A decision stores the revision OPA reported; replay only means something if
    the bundle we load reports that same revision back. `policy/revision/data.json`
    travels inside the tarball for exactly this reason, so a swapped bundle
    announces itself instead of quietly reproducing the wrong policy.
    """
    revision, bundle = pinned
    with BundleServer(bundle, opa_binary) as server, PolicyClient(base_url=server.url) as client:
        decision = client.evaluate(_hard_input())
    assert decision.bundle_revision == revision


def test_bundle_for_resolves_a_committed_revision(pinned):
    revision, bundle = pinned
    assert bundle_for(revision) == bundle
    assert bundle_for("0000000000000000") is None


# ─────────────────────────────────────────────────────────────────────────────
# The positive claim
# ─────────────────────────────────────────────────────────────────────────────
def test_every_decision_reproduces_against_its_pinned_bundle(live_ledger):
    """Demo Beat 5. Not "the field is present" -- the verdict is re-derived."""
    conn = connect(live_ledger)
    try:
        report = replay_ledger(conn)
    finally:
        conn.close()

    assert report.ran, "replay did not run; the claim would be unproven"
    assert report.total > 0
    assert report.unreplayable == 0, "a decision stored no input and cannot be proven"
    assert report.divergences == [], report.render()
    assert report.reproduced == report.total
    assert report.ok


def test_every_decision_stores_the_input_that_authorised_it(live_ledger):
    """The regression that made replay impossible.

    `evaluate_ladder` built its LadderOutcome separately at each return, and the
    allow path omitted `policy_inputs`. So T1/T2/T3 -- every decision that ever
    reached actuation -- stored `{}`, and the rows that authorised money were the
    only rows that could not be re-derived.
    """
    conn = connect(live_ledger)
    try:
        empty = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type = 'DECISION' "
            "AND (policy_input_json IN ('{}', '') OR policy_input_json IS NULL)"
        ).fetchone()[0]
        actuated_without_input = conn.execute(
            "SELECT COUNT(*) FROM ledger d JOIN ledger a ON a.decision_seq = d.seq "
            "AND a.entry_type = 'ACTUATION' AND a.executed = 1 "
            "WHERE d.entry_type = 'DECISION' AND d.policy_input_json IN ('{}', '')"
        ).fetchone()[0]
    finally:
        conn.close()

    assert empty == 0
    assert actuated_without_input == 0


def test_recorded_verdict_describes_the_recorded_input(live_ledger):
    """The input, the verdict and the deny-set must describe ONE tier.

    `opa_allow` is what POLICY said about the stored input, not whether we acted.
    Conflating the two is what made every T0 and T4 row unreplayable: T4 is
    authorised but is not a money action, so it allows without actuating.

    T0 is never queried -- doing nothing needs no authorisation -- so a
    terminated decline records the first tier we DID ask about, and the stored
    input names that tier, which keeps the row self-describing.
    """
    conn = connect(live_ledger)
    try:
        rows = conn.execute(
            "SELECT tier, opa_allow, policy_input_json FROM ledger WHERE entry_type = 'DECISION'"
        ).fetchall()
    finally:
        conn.close()

    assert rows
    for tier, allow, raw in rows:
        recorded_tier = json.loads(raw)["tier"]
        assert recorded_tier in ("T1", "T2", "T3", "T4"), "T0 is never queried"
        if tier in ("T1", "T2", "T3"):
            assert recorded_tier == tier
            assert allow == 1, "we acted on this tier, so policy must have allowed it"
        if tier == "T0":
            assert allow == 0, "nothing was authorised, or we would not have terminated"


# ─────────────────────────────────────────────────────────────────────────────
# The negative claims -- what replay catches that the chain cannot
# ─────────────────────────────────────────────────────────────────────────────
def _hard_input() -> dict:
    """A policy input the kernel denies for every actionable tier."""
    ctx = DeclineContext(
        cause="LOST_STOLEN_FRAUD",
        max_posterior=0.99,
        rail="card",
        amount_paise=250_000,
        network_category=None,
        merchant_advice_code=None,
        npci_retry_remark=None,
        attempts_30d=0,
        attempts_this_payment=0,
        bin_attempts_1h=0,
        customer_nudges_7d=0,
        is_emandate=False,
        afa_completed=False,
        ms_since_pre_debit_notice=90_000_000,
        ist_hour=9,
        has_alternate_instrument=True,
    )
    return build_policy_input(ctx, "T1")


def test_replay_detects_a_forged_verdict(tmp_path, pinned, opa_binary):
    """The attacker the hash chain cannot see.

    They hold the append path, so they write through the normal writer and the
    chain is perfect. They simply record `allow` for an input the policy denies.
    `verify` on the chain alone passes. Replay is the only thing that catches it.
    """
    revision, _bundle = pinned
    path = tmp_path / "forged.db"
    conn = connect(path)
    try:
        from praman.ledger.chain import append

        append(
            conn,
            DecisionRecord(
                ts_ms=1_787_000_000_000,
                experiment_id="forgery",
                holdout_pct=20,
                payment_id="pay_FORGED",
                customer_id="cust_00001",
                arm="treatment",
                attempt_no=1,
                rail="card",
                symbol="41",
                region="IN",
                cause="LOST_STOLEN_FRAUD",
                posterior={"LOST_STOLEN_FRAUD": 0.99},
                attribution_source="heuristic",
                attribution_version="taxonomy-v1",
                tier="T1",
                tier_evaluations={"T1": []},
                opa_allow=True,  # <- the lie
                deny_reasons=[],
                policy_input=_hard_input(),
                bundle_revision=revision,
                decision_id="forged",
                amount_paise=250_000,
                cuped_covariate=0.5,
                covariate_asof_ms=1_786_000_000_000,
            ).to_row(),
        )

        chain_ok, _broken, _msg = verify(conn)
        assert chain_ok, "the forged row was appended normally; the chain must still pass"

        report = replay_ledger(conn)
    finally:
        conn.close()

    assert report.ran
    assert not report.ok
    assert report.diverged == 1
    assert {d.attribute for d in report.divergences} >= {"allow"}


def test_replay_detects_a_mutated_policy_input(live_ledger, tmp_path):
    """Mutate one stored input, then repair the chain around it.

    Tampering alone breaks the hash, so the chain already catches the careless
    attacker. The interesting one drops the trigger, rewrites the row, AND
    re-derives every downstream hash -- at which point `verify`'s chain check is
    perfectly happy. The stored input no longer produces the recorded verdict,
    and only replay can tell.
    """
    import shutil

    path = tmp_path / "mutated.db"
    shutil.copy(live_ledger, path)

    conn = connect(path)
    try:
        seq, stored = _first_decision(conn)
        # Drive attempts past the network cap: same shape, different verdict.
        stored["attempts_30d"] = 999
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_update")
        conn.execute(
            "UPDATE ledger SET policy_input_json = ? WHERE seq = ?",
            (json.dumps(stored, sort_keys=True, separators=(",", ":")), seq),
        )
    finally:
        conn.close()

    _rechain(path, from_seq=seq)

    conn = connect(path)
    try:
        chain_ok, _broken, msg = verify(conn)
        assert chain_ok, f"the attacker repaired the chain; it must pass: {msg}"
        report = replay_ledger(conn)
    finally:
        conn.close()

    assert report.ran
    assert not report.ok, "a mutated input reproduced the old verdict -- replay is decorative"
    assert any(d.seq == seq for d in report.divergences)


def test_an_unstored_input_is_not_counted_as_proof(live_ledger, tmp_path):
    """A decision with no stored input is UNPROVEN, never reproduced.

    The tempting bug is to skip such rows and still print "N/N reproduced".
    That is precisely the claim this command exists to stop making.
    """
    import shutil

    path = tmp_path / "hollow.db"
    shutil.copy(live_ledger, path)

    conn = connect(path)
    try:
        seq, _stored = _first_decision(conn)
        conn.execute("DROP TRIGGER IF EXISTS ledger_no_update")
        conn.execute("UPDATE ledger SET policy_input_json = '{}' WHERE seq = ?", (seq,))
    finally:
        conn.close()

    _rechain(path, from_seq=seq)

    conn = connect(path)
    try:
        report = replay_ledger(conn)
    finally:
        conn.close()

    assert report.unreplayable == 1
    assert report.reproduced == report.total - 1
    assert not report.ok


def test_a_missing_bundle_is_skipped_not_silently_passed(live_ledger, tmp_path):
    """If the pinned bundle is not committed, the decision cannot be replayed --
    and the report must say so instead of reporting success over zero work."""
    conn = connect(live_ledger)
    try:
        report = replay_ledger(conn, bundle_dir=tmp_path)
    finally:
        conn.close()

    assert not report.ran
    assert report.reproduced == 0
    assert report.skipped
    assert "not committed" in next(iter(report.skipped.values()))
    assert "skipped" in report.render()


def test_report_never_claims_reproduction_without_replaying():
    """The exact bug this module replaces: `verify` printed
    "N/N decisions reproduced" off a GROUP BY, having reproduced nothing."""
    from praman.ledger.replay import ReplayReport

    empty = ReplayReport(total=3000)
    assert not empty.ran
    assert "reproduced" not in empty.render()
