"""Archetype cache.

Explanations are not per-payment. Two declines with the same cause, the same
tier, the same deny-set and the same confidence band get the same words, because
the same things are true about them. So the cache key is the ARCHETYPE, not the
payment -- keying on `payment_id` would make every decision a miss and every
demo a sequence of network round trips.

    sha256(cause | tier | sorted(deny_reasons) | confidence_bucket)

Sorted, because a deny-set is a set and its iteration order is not information.
Bucketed, because 0.79 and 0.81 are the same story and keying on the raw float
would defeat the whole mechanism.

Persisted in SQLite rather than held in memory so a pre-warm survives process
restarts. The demo is pre-warmed before recording; nothing on the golden path
should ever wait on an external API.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from praman.explain.template import DecisionSummary

# Coarse enough that neighbouring confidences share an entry, fine enough that
# "unsure" and "confident" never do -- the boundary at 0.4 is the Rego
# confidence floor, so the two sides genuinely tell different stories.
CONFIDENCE_BUCKETS: tuple[float, ...] = (0.4, 0.7, 0.9)


def confidence_bucket(p: float) -> str:
    for i, edge in enumerate(CONFIDENCE_BUCKETS):
        if p < edge:
            return f"b{i}"
    return f"b{len(CONFIDENCE_BUCKETS)}"


def archetype_key(summary: DecisionSummary) -> str:
    material = "|".join(
        [
            summary.cause,
            summary.tier,
            ",".join(sorted(summary.deny_reasons)),
            confidence_bucket(summary.confidence),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ArchetypeCache:
    """SQLite-backed, so a pre-warm survives a restart."""

    __slots__ = ("_path",)

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS explanations ("
                "  key TEXT PRIMARY KEY, text TEXT NOT NULL,"
                "  created_at INTEGER DEFAULT (strftime('%s','now')))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), isolation_level=None)

    def get(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT text FROM explanations WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO explanations (key, text) VALUES (?, ?)", (key, text)
            )

    def __len__(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM explanations").fetchone()[0])


__all__ = ["CONFIDENCE_BUCKETS", "ArchetypeCache", "archetype_key", "confidence_bucket"]
