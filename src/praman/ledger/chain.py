"""The hash chain itself.

`entry_hash = sha256(prev_hash || canonical_bytes(row))`.

The interesting part is not the hashing, it is the concurrency. Reading the head
and inserting against it must be ONE critical section, or two concurrent writers
fork the chain (S1). `BEGIN IMMEDIATE` takes SQLite's RESERVED lock before the
read, which is what makes the sequence atomic. `UNIQUE(prev_hash)` is the belt to
that braces: if the lock were ever wrong, the database refuses the fork instead
of storing it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

from praman.ledger.canonical import GENESIS, canonical_bytes
from praman.metrics import LEDGER_ENTRIES, LEDGER_FORK_ATTEMPTS

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Frozen. The hash covers exactly these columns, in this order. Changing this
# tuple invalidates every existing ledger, so it is append-at-the-end only.
FIELDS: tuple[str, ...] = (
    "ts_ms",
    "payment_id",
    "customer_id",
    "arm",
    "cause",
    "posterior",
    "tier",
    "opa_allow",
    "deny_reasons",
    "bundle_revision",
    "decision_id",
    "amount_paise",
    "payload_json",
)

# High enough that concurrent writers queue rather than fail. Each append is
# sub-millisecond, so the real wait is tiny; this is headroom, not latency.
_BUSY_TIMEOUT_MS = 15_000

# ── Two-level serialisation (S1) ────────────────────────────────────────────
# BEGIN IMMEDIATE alone is NOT sufficient in a threaded process. SQLite's busy
# handler is deliberately bypassed when it detects a potential deadlock between
# connections in the same process, so heavy in-process contention surfaces as
# `database is locked` rather than waiting. The fix is the one the design calls
# for: an in-process lock for threads, BEGIN IMMEDIATE for other processes.
#
# Locks are per resolved path, so two different ledgers never block each other.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _lock_for(conn: sqlite3.Connection) -> threading.Lock:
    row = conn.execute("PRAGMA database_list").fetchone()
    key = str(Path(row[2]).resolve()) if row and row[2] else "::memory::"
    with _REGISTRY_LOCK:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
    return lock


def _has_schema(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger'").fetchone()
        is not None
    )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a ledger connection, creating the schema only if it is missing.

    `isolation_level=None` puts the driver in autocommit mode so that we control
    transactions explicitly. Without it, Python's sqlite3 module inserts its own
    BEGIN and our BEGIN IMMEDIATE never takes the lock we need.

    The schema check is not an optimisation. `executescript` issues an implicit
    COMMIT and takes a write lock, so running it unconditionally means every new
    connection fights for the write lock just to open. Under real concurrency
    that surfaces as `database is locked` on connect -- the appends themselves
    were never the contended resource.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    # Busy timeout first, so even schema creation retries instead of failing.
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if not _has_schema(conn):
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def entry_hash(prev_hash: str, row: dict[str, Any]) -> str:
    material = prev_hash.encode("utf-8") + canonical_bytes({k: row[k] for k in FIELDS})
    return hashlib.sha256(material).hexdigest()


def head_hash(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    return r[0] if r else GENESIS


def append(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    """Atomically append one entry and return its hash.

    Law #4: this must happen BEFORE the side effect it describes. A decision that
    was actuated but not recorded is exactly the thing the audit trail exists to
    make impossible.
    """
    missing = [f for f in FIELDS if f not in row]
    if missing:
        raise KeyError(f"ledger row missing required fields: {missing}")

    with _lock_for(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            prev = head_hash(conn)
            h = entry_hash(prev, row)
            placeholders = ",".join("?" * (len(FIELDS) + 2))
            conn.execute(
                f"INSERT INTO ledger ({','.join(FIELDS)},prev_hash,entry_hash) "
                f"VALUES ({placeholders})",
                (*(row[k] for k in FIELDS), prev, h),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            # UNIQUE(prev_hash) fired: another writer claimed this head first.
            # The chain did not fork -- the database refused to let it.
            LEDGER_FORK_ATTEMPTS.inc()
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

    LEDGER_ENTRIES.inc()
    return h


def verify(conn: sqlite3.Connection) -> tuple[bool, int | None, str]:
    """Recompute the entire chain. Returns (ok, first_broken_seq, message)."""
    prev = GENESIS
    rows = conn.execute(
        f"SELECT seq,{','.join(FIELDS)},prev_hash,entry_hash FROM ledger ORDER BY seq"
    ).fetchall()

    for i, r in enumerate(rows):
        seq, *vals, stored_prev, stored_hash = r

        if seq != i + 1:
            return False, seq, f"sequence gap at entry {seq}"
        if stored_prev != prev:
            return False, seq, f"prev_hash mismatch at entry {seq}"

        expected = entry_hash(prev, dict(zip(FIELDS, vals, strict=True)))
        if expected != stored_hash:
            remaining = len(rows) - i - 1
            return (
                False,
                seq,
                (
                    f"CHAIN BROKEN at entry {seq} "
                    f"(expected {expected[:8]}... got {stored_hash[:8]}...) "
                    f"-> {remaining} subsequent entries invalidated"
                ),
            )
        prev = stored_hash

    return True, None, f"chain intact ({len(rows)} entries, head {prev[:8]}...)"


__all__ = ["FIELDS", "append", "connect", "entry_hash", "head_hash", "verify"]
