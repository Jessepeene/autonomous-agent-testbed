"""Scheduler helpers that need no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from factory.supervisor.scheduler import _bucket


def test_bucket_is_stable_within_a_window() -> None:
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    later_same_hour = now + timedelta(minutes=45)
    assert _bucket(now, 3600) == _bucket(later_same_hour, 3600)


def test_bucket_changes_across_a_window_boundary() -> None:
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    next_hour = now + timedelta(hours=1, minutes=1)
    assert _bucket(now, 3600) != _bucket(next_hour, 3600)


def test_bucket_clamps_a_misconfigured_interval_to_one_second() -> None:
    """A zero or negative interval must not raise ZeroDivisionError or invert the
    windowing — it degrades to "every second" rather than crashing the tick."""
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    expected = str(int(now.timestamp()))
    assert _bucket(now, 0) == expected
    assert _bucket(now, -5) == expected
