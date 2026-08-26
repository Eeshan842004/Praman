"""Hash-chained, append-only ledger.

Two failures here are fatal AND silent, so they get proven dead now rather than
discovered on Day 9:

  S1 — hash-chain fork. Two concurrent appends read the same head, both compute
       against it, both insert. That is a fork, not a chain. It corrupts nothing
       visibly; `verify` simply fails a week later with no obvious cause.

  C4 — float drift. 0.1 + 0.2 == 0.30000000000000004. Any float re-serialisation
       changes the canonical bytes and breaks every downstream hash.

Written before the implementation exists.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from praman.ledger.canonical import GENESIS, canonical_bytes, prob_str
from praman.ledger.chain import append, connect, head_hash, verify


# ─────────────────────────────────────────────────────────────────────────────
# Canonical serialisation (C4)
# ─────────────────────────────────────────────────────────────────────────────
def test_canonical_bytes_is_key_order_independent():
    a = {"b": 2, "a": 1, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 1, "b": 2}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_is_stable_across_calls():
    obj = {"payment_id": "pay_x", "amount_paise": 2200000, "ts_ms": 1787000000000}
    assert canonical_bytes(obj) == canonical_bytes(obj)


def test_canonical_bytes_has_no_incidental_whitespace():
    assert canonical_bytes({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


def test_canonical_bytes_preserves_unicode_without_escaping():
    """Bank strings contain non-ASCII. Escaping them would still be stable, but
    ensure_ascii=False keeps the bytes readable in the committed evidence file."""
    out = canonical_bytes({"desc": "शेष राशि अपर्याप्त"})
    assert "शेष".encode() in out


@pytest.mark.parametrize(
    "payload",
    [
        0.1,
        {"amount": 1.5},
        {"nested": {"p": 0.30000000000000004}},
        {"list": [1, 2, 3.0]},
        [0.5],
        {"deep": {"deeper": [{"x": 2.5}]}},
    ],
)
def test_float_anywhere_is_rejected(payload):
    """Law #5. Money = int paise, probability = 6-dp string, time = epoch ms int."""
    with pytest.raises(TypeError, match="float"):
        canonical_bytes(payload)


def test_ints_and_strings_and_bools_and_none_are_accepted():
    canonical_bytes({"i": 1, "s": "x", "b": True, "n": None, "l": [1, "2"], "d": {"k": 3}})


def test_prob_str_is_six_dp_and_round_trips_stably():
    assert prob_str(0.1 + 0.2) == "0.300000"
    assert prob_str(0.5603) == "0.560300"
    assert prob_str(1.0) == "1.000000"
    assert prob_str(0.0) == "0.000000"
    # The output must itself be ledger-safe.
    canonical_bytes({"posterior": prob_str(0.9938)})


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
def _row(n: int = 1) -> dict:
    return {
        "ts_ms": 1787000000000 + n,
        "payment_id": f"pay_TEST{n:06d}",
        "customer_id": f"cust_{n % 37:04d}",
        "arm": "treatment" if n % 10 else "holdout",
        "cause": "INSUFFICIENT_FUNDS",
        "posterior": prob_str(0.56 + (n % 5) / 1000),
        "tier": "T1",
        "opa_allow": 1,
        "deny_reasons": "[]",
        "bundle_revision": "4ca4787c0a1eea75",
        "decision_id": f"dec_{n:06d}",
        "amount_paise": 100000 + n,
        "payload_json": '{"redacted":true}',
    }


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "ledger.db")
    yield conn
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Chain integrity
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_ledger_verifies_and_head_is_genesis(db):
    ok, broken_at, _ = verify(db)
    assert ok and broken_at is None
    assert head_hash(db) == GENESIS


def test_single_append_links_to_genesis(db):
    h = append(db, _row(1))
    row = db.execute("SELECT prev_hash, entry_hash FROM ledger").fetchone()
    assert row[0] == GENESIS
    assert row[1] == h
    assert verify(db)[0]


def test_chain_of_many_verifies(db):
    for n in range(1, 51):
        append(db, _row(n))
    ok, broken_at, msg = verify(db)
    assert ok, msg
    assert broken_at is None
    assert "50" in msg


def test_each_entry_links_to_its_predecessor(db):
    for n in range(1, 11):
        append(db, _row(n))
    rows = db.execute("SELECT seq, prev_hash, entry_hash FROM ledger ORDER BY seq").fetchall()
    assert rows[0][1] == GENESIS
    for prev, cur in itertools.pairwise(rows):
        assert cur[1] == prev[2], f"entry {cur[0]} does not link to {prev[0]}"


# ─────────────────────────────────────────────────────────────────────────────
# Append-only enforcement — storage level, not convention
# ─────────────────────────────────────────────────────────────────────────────
def test_update_is_blocked_by_trigger(db):
    append(db, _row(1))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE ledger SET amount_paise = 999 WHERE seq = 1")


def test_delete_is_blocked_by_trigger(db):
    append(db, _row(1))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM ledger WHERE seq = 1")


def test_duplicate_prev_hash_is_rejected(db):
    """UNIQUE(prev_hash) turns a fork into a constraint violation at insert time
    rather than silent corruption discovered a week later."""
    append(db, _row(1))
    prev = GENESIS
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO ledger (ts_ms,payment_id,customer_id,arm,cause,posterior,tier,"
            "opa_allow,deny_reasons,bundle_revision,decision_id,amount_paise,payload_json,"
            "prev_hash,entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "p",
                "c",
                "treatment",
                "X",
                "0.500000",
                "T1",
                1,
                "[]",
                "r",
                "d",
                1,
                "{}",
                prev,
                "deadbeef" * 8,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tamper detection — the demo weapon
# ─────────────────────────────────────────────────────────────────────────────
def test_tampering_breaks_the_chain_at_the_exact_entry(db):
    for n in range(1, 21):
        append(db, _row(n))
    assert verify(db)[0]

    # Tampering requires dropping the append-only trigger first: a privileged,
    # visible act. The chain must catch it anyway.
    db.execute("DROP TRIGGER ledger_no_update")
    db.execute("UPDATE ledger SET amount_paise = 99999900 WHERE seq = 12")
    db.commit()

    ok, broken_at, msg = verify(db)
    assert not ok
    assert broken_at == 12
    assert "CHAIN BROKEN" in msg
    assert "8" in msg  # 20 - 12 = 8 subsequent entries invalidated


@pytest.mark.parametrize(
    "field", ["payment_id", "cause", "posterior", "tier", "amount_paise", "bundle_revision"]
)
def test_every_hashed_field_is_actually_covered(db, field):
    """A field that can be changed without breaking the chain is not evidence."""
    for n in range(1, 6):
        append(db, _row(n))
    db.execute("DROP TRIGGER ledger_no_update")
    new = 424242 if field == "amount_paise" else "TAMPERED"
    db.execute(f"UPDATE ledger SET {field} = ? WHERE seq = 3", (new,))
    db.commit()
    ok, broken_at, _ = verify(db)
    assert not ok, f"{field} is not covered by the hash"
    assert broken_at == 3


# ─────────────────────────────────────────────────────────────────────────────
# S1 — concurrency. The fork must be structurally impossible.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n_writers", [200])
def test_concurrent_appends_never_fork_the_chain(tmp_path: Path, n_writers: int):
    """
    200 threads, 200 independent connections, all appending at once.

    BEGIN IMMEDIATE takes the RESERVED lock BEFORE reading the head, making
    read-head -> compute -> insert a single critical section. Without it, two
    writers read the same head and fork.

    Asserted: every append lands, the chain verifies, and seq is gapless.
    """
    path = tmp_path / "concurrent.db"
    connect(path).close()  # create schema once

    barrier = threading.Barrier(n_writers)
    errors: list[Exception] = []

    def writer(n: int) -> None:
        conn = connect(path)
        try:
            barrier.wait(timeout=30)  # maximise real contention
            append(conn, _row(n))
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=n_writers) as pool:
        list(pool.map(writer, range(1, n_writers + 1)))

    assert not errors, f"{len(errors)} writers failed: {errors[:3]}"

    conn = connect(path)
    try:
        ok, broken_at, msg = verify(conn)
        assert ok, f"chain broken at {broken_at}: {msg}"

        seqs = [r[0] for r in conn.execute("SELECT seq FROM ledger ORDER BY seq")]
        assert seqs == list(range(1, n_writers + 1)), "sequence is not gapless"

        prevs = [r[0] for r in conn.execute("SELECT prev_hash FROM ledger")]
        assert len(set(prevs)) == n_writers, "a prev_hash was reused -- that is a fork"
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Property-based: no sequence of valid entries can produce an invalid chain
# ─────────────────────────────────────────────────────────────────────────────
_ledger_row = st.builds(
    lambda pid, cid, amt, ts, tier, allow: {
        "ts_ms": ts,
        "payment_id": pid,
        "customer_id": cid,
        "arm": "treatment",
        "cause": "INSUFFICIENT_FUNDS",
        "posterior": prob_str(0.5),
        "tier": tier,
        "opa_allow": allow,
        "deny_reasons": "[]",
        "bundle_revision": "rev",
        "decision_id": pid,
        "amount_paise": amt,
        "payload_json": "{}",
    },
    pid=st.text(min_size=1, max_size=24),
    cid=st.text(min_size=1, max_size=24),
    amt=st.integers(min_value=0, max_value=10**12),
    ts=st.integers(min_value=0, max_value=2**53),
    tier=st.sampled_from(["T0", "T1", "T2", "T3", "T4"]),
    allow=st.integers(min_value=0, max_value=1),
)


@given(rows=st.lists(_ledger_row, min_size=0, max_size=40))
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_append_all_then_verify_always_holds(tmp_path_factory, rows):
    path = tmp_path_factory.mktemp("prop") / "l.db"
    conn = connect(path)
    try:
        for r in rows:
            append(conn, r)
        ok, broken_at, msg = verify(conn)
        assert ok, f"broken at {broken_at}: {msg}"
        assert head_hash(conn) == (
            GENESIS
            if not rows
            else conn.execute("SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()[
                0
            ]
        )
    finally:
        conn.close()
