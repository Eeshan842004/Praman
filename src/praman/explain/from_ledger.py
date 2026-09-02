"""Build explanation inputs from recorded decisions.

The explanation is a VIEW over the ledger. It reads a row that was written
before anything was actuated and is covered by the hash chain, so the prose and
the attestation describe the same event by construction -- there is no second
store that could drift from the evidence.
"""

from __future__ import annotations

import json
import sqlite3

from praman.explain.template import ACTION_TEXT, DecisionSummary

_TIER_ACTION = {
    "T0": "terminate",
    "T1": "silent_retry",
    "T2": "rail_switch",
    "T3": "customer_nudge",
    "T4": "human_escalate",
}

_QUERY = """
SELECT seq, payment_id, cause, posterior, tier, tier_evaluations,
       deny_reasons, amount_paise, rail, bundle_revision
FROM   ledger
WHERE  entry_type = 'DECISION'
"""


def _row_to_summary(row: tuple) -> DecisionSummary:
    (seq, payment_id, cause, posterior, tier, evaluations, deny, amount, rail, revision) = row
    return DecisionSummary(
        payment_id=payment_id,
        cause=cause,
        # Probabilities are stored as 6-dp strings (law #5), never floats.
        confidence=float(posterior or 0.0),
        tier=tier,
        action=_TIER_ACTION.get(tier, tier),
        amount_paise=int(amount or 0),
        rail=rail or "card",
        deny_reasons=json.loads(deny or "[]"),
        tier_evaluations=json.loads(evaluations or "{}"),
        bundle_revision=revision or "",
        ledger_seq=int(seq),
    )


def summary_at(conn: sqlite3.Connection, seq: int) -> DecisionSummary | None:
    row = conn.execute(f"{_QUERY} AND seq = ?", (seq,)).fetchone()
    return _row_to_summary(row) if row else None


def all_summaries(conn: sqlite3.Connection, limit: int = 5000) -> list[DecisionSummary]:
    rows = conn.execute(f"{_QUERY} ORDER BY seq LIMIT ?", (limit,)).fetchall()
    return [_row_to_summary(r) for r in rows]


def deadlock_summaries(conn: sqlite3.Connection, limit: int = 20) -> list[DecisionSummary]:
    """Decisions where several regulators blocked every automated tier.

    The highest-signal rows in the ledger, and the ones a demo actually shows.
    """
    out = []
    for summary in all_summaries(conn):
        blocked = [t for t, r in summary.tier_evaluations.items() if r and t != "T4"]
        if len(blocked) >= 3 and summary.tier == "T4":
            out.append(summary)
        if len(out) >= limit:
            break
    return out


__all__ = ["ACTION_TEXT", "all_summaries", "deadlock_summaries", "summary_at"]
