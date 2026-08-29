"""The delivery log: idempotency and the work queue.

Kept in its OWN database, not in the ledger. The ledger is evidence -- append
only, hash chained, every column inside the hash. This table has a mutable
`processed` flag and rows that get skipped, so putting it in the same file would
invite exactly the question the evidence file exists to close.

The primary key is (event_id, payment_id), which is the whole of S2's defence.
Razorpay retries until it sees a 2xx, so the same event id arrives repeatedly;
INSERT OR IGNORE makes the second arrival a no-op, and the caller enqueues only
when the insert actually took. Deduplicating on payment_id alone would be wrong
in the other direction -- one payment legitimately produces several distinct
events.

Law #7 is upstream of this file: a delivery is an OBSERVATION, never an attempt.
Nothing here touches a compliance counter.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id      TEXT NOT NULL,
    payment_id    TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    raw_json      TEXT NOT NULL,
    processed     INTEGER NOT NULL DEFAULT 0,
    decision_seq  INTEGER,
    PRIMARY KEY (event_id, payment_id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_pending ON webhook_events(processed, received_at_ms);
"""

_BUSY_TIMEOUT_MS = 5_000


def connect_ingest(path: str | Path) -> sqlite3.Connection:
    """Open the delivery log. WAL, because the acknowledging writer and the
    processing reader must not block each other -- a reader holding up an ack is
    the exact failure this layer is built to avoid."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because the ASGI server may hand the handler to a
    # different thread than the one that built the router. Access is serialised
    # by the caller's lock; SQLite itself is compiled thread-safe.
    conn = sqlite3.connect(
        str(path),
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


def record_delivery(
    conn: sqlite3.Connection,
    event_id: str,
    payment_id: str,
    received_at_ms: int,
    raw_json: str,
) -> bool:
    """Record a delivery. True if it was NEW and should be enqueued.

    The return value is the enqueue decision, and it comes from the database's
    own uniqueness constraint rather than from a preceding SELECT. A
    check-then-insert would race two concurrent redeliveries into two enqueues,
    which is precisely the duplicate this table exists to stop.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO webhook_events "
        "(event_id, payment_id, received_at_ms, raw_json) VALUES (?, ?, ?, ?)",
        (event_id, payment_id, int(received_at_ms), raw_json),
    )
    return cur.rowcount == 1


def pending(conn: sqlite3.Connection, limit: int = 1000) -> list[sqlite3.Row]:
    """Deliveries awaiting processing, oldest first."""
    return conn.execute(
        "SELECT * FROM webhook_events WHERE processed = 0 "
        "ORDER BY received_at_ms, event_id LIMIT ?",
        (limit,),
    ).fetchall()


def mark_processed(
    conn: sqlite3.Connection, event_id: str, payment_id: str, decision_seq: int | None = None
) -> None:
    """Close out a delivery, recording which ledger decision it produced.

    `decision_seq` is the join between an observation and the evidence chain: it
    is what lets an auditor walk from a webhook Razorpay sent to the decision it
    caused.
    """
    conn.execute(
        "UPDATE webhook_events SET processed = 1, decision_seq = ? "
        "WHERE event_id = ? AND payment_id = ?",
        (decision_seq, event_id, payment_id),
    )


__all__ = ["connect_ingest", "mark_processed", "pending", "record_delivery"]
