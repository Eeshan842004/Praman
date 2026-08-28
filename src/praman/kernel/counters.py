"""Windowed compliance counters.

Rego has no database and no clock, so every time-window aggregate is computed
here and handed to the policy as `input`. That keeps each policy test a pure
function of its input -- and puts the entire burden of getting the WINDOW right
on this module.

Law #7 lives here too: only ACTUATIONS are recorded. A decision the policy
refused is not an attempt, and a webhook redelivery is an observation, not an
attempt.
"""

from __future__ import annotations

import bisect
from collections import defaultdict


class WindowedCounters:
    """Counts events per key within a trailing time window.

    Timestamps are kept sorted per key so a count is a binary search rather than
    a scan. Windows are half-open on the left: an event exactly `window_ms` old
    still counts, one millisecond older does not.
    """

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: dict[str, list[int]] = defaultdict(list)

    def record(self, key: str, ts_ms: int) -> None:
        """Record one event. Insertion keeps the list sorted, so out-of-order
        arrivals are handled -- declines arrive sorted today, but nothing in the
        contract promises that."""
        bisect.insort(self._events[key], int(ts_ms))

    def count(self, key: str, now_ms: int, window_ms: int) -> int:
        """Events in [now_ms - window_ms, now_ms]."""
        events = self._events.get(key)
        if not events:
            return 0
        lo = bisect.bisect_left(events, int(now_ms) - int(window_ms))
        hi = bisect.bisect_right(events, int(now_ms))
        return hi - lo

    def total(self, key: str) -> int:
        """Lifetime count. Correct for per-payment attempt ceilings, which are
        genuinely cumulative rather than windowed."""
        return len(self._events.get(key, ()))


HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WINDOW_1H = HOUR_MS
WINDOW_7D = 7 * DAY_MS
WINDOW_30D = 30 * DAY_MS

__all__ = ["DAY_MS", "HOUR_MS", "WINDOW_1H", "WINDOW_7D", "WINDOW_30D", "WindowedCounters"]
