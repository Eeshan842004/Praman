"""Windowed compliance counters.

Rego has no database and no clock, so every time-window aggregate is computed
here and passed in as `input`. That is the correct design -- and it puts the
entire burden of getting the WINDOW right on this module.

The bug this file exists to prevent was real and live: the counters were
cumulative, not windowed. A field named `bin_attempts_1h` carried a running
total for the whole batch, so once a BIN crossed ten actuations every later
decline on it was denied forever. 81% of a 3000-decline batch terminated, and
`bin_velocity` fired 8560 times.

A cumulative counter behind a windowed name does not fail loudly. It quietly
strangles the system while every individual component reports success.

Written before the implementation exists.
"""

from __future__ import annotations

from praman.kernel.counters import WindowedCounters

HOUR = 3_600_000
DAY = 86_400_000


def test_counts_are_zero_before_anything_is_recorded():
    c = WindowedCounters()
    assert c.count("bin:123", now_ms=0, window_ms=HOUR) == 0


def test_events_inside_the_window_are_counted():
    c = WindowedCounters()
    for i in range(5):
        c.record("bin:123", ts_ms=1000 * i)
    assert c.count("bin:123", now_ms=10_000, window_ms=HOUR) == 5


def test_events_outside_the_window_are_not_counted():
    """The whole point. Two actuations two days apart are not two attempts in
    an hour, and treating them as such denies a legal action."""
    c = WindowedCounters()
    c.record("bin:123", ts_ms=0)
    c.record("bin:123", ts_ms=2 * DAY)
    assert c.count("bin:123", now_ms=2 * DAY, window_ms=HOUR) == 1


def test_a_month_of_spread_out_attempts_never_trips_an_hourly_cap():
    """Regression for the live bug: 30 attempts spread over 30 days must read as
    at most one in any given hour."""
    c = WindowedCounters()
    for d in range(30):
        c.record("bin:123", ts_ms=d * DAY)
    assert c.count("bin:123", now_ms=29 * DAY, window_ms=HOUR) == 1
    assert c.count("bin:123", now_ms=29 * DAY, window_ms=30 * DAY) == 30


def test_window_is_half_open_so_the_boundary_does_not_double_count():
    c = WindowedCounters()
    c.record("k", ts_ms=0)
    assert c.count("k", now_ms=HOUR, window_ms=HOUR) == 1  # exactly at the edge
    assert c.count("k", now_ms=HOUR + 1, window_ms=HOUR) == 0


def test_keys_are_independent():
    c = WindowedCounters()
    c.record("bin:1", ts_ms=0)
    c.record("bin:2", ts_ms=0)
    assert c.count("bin:1", now_ms=0, window_ms=HOUR) == 1


def test_out_of_order_records_still_count_correctly():
    """Declines arrive sorted today, but nothing in the contract promises it."""
    c = WindowedCounters()
    for ts in (5 * HOUR, 1 * HOUR, 3 * HOUR):
        c.record("k", ts_ms=ts)
    assert c.count("k", now_ms=5 * HOUR, window_ms=3 * HOUR) == 2


def test_thirty_day_and_seven_day_windows_are_independent_views():
    c = WindowedCounters()
    for d in (0, 5, 10, 25, 29):
        c.record("cust:1", ts_ms=d * DAY)
    assert c.count("cust:1", now_ms=29 * DAY, window_ms=7 * DAY) == 2  # days 25, 29
    assert c.count("cust:1", now_ms=29 * DAY, window_ms=30 * DAY) == 5
