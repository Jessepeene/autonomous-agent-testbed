"""Deterministic ledger anomaly detection (Tier 0 of the system-wide
verification plan) -- pure functions, no DB, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime

from factory.engines.finance.anomaly import (
    MIN_DAYS_FOR_BASELINE,
    daily_totals,
    detect_anomaly,
)


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


# --- daily_totals ----------------------------------------------------------------


def test_daily_totals_sums_multiple_entries_on_the_same_day() -> None:
    rows = [(_at(1, 9), 1000), (_at(1, 15), 500), (_at(2, 9), 200)]

    totals = daily_totals(rows)

    assert totals[_at(1).date()] == 1500
    assert totals[_at(2).date()] == 200


def test_daily_totals_of_an_empty_series_is_empty() -> None:
    assert daily_totals([]) == {}


# --- detect_anomaly ----------------------------------------------------------------


def test_not_enough_history_never_flags_anything() -> None:
    """A young business with fewer than MIN_DAYS_FOR_BASELINE days of history
    has no real baseline yet -- silence, not a false alarm."""
    short_history = [1000] * (MIN_DAYS_FOR_BASELINE - 1)

    assert detect_anomaly(short_history, today_cents=1_000_000) is None


def test_a_steady_history_with_an_ordinary_day_is_not_flagged() -> None:
    steady_history = [10_000, 10_500, 9_800, 10_200, 9_900, 10_100]

    assert detect_anomaly(steady_history, today_cents=10_300) is None


def test_a_wild_spike_relative_to_a_steady_history_is_flagged() -> None:
    steady_history = [10_000, 10_500, 9_800, 10_200, 9_900, 10_100]

    reason = detect_anomaly(steady_history, today_cents=500_000)

    assert reason is not None
    assert "deviates" in reason


def test_a_wild_drop_relative_to_a_steady_history_is_flagged() -> None:
    """Not just spikes -- an implausible drop (e.g. revenue collapsing to
    near-zero) is exactly as worth a human's attention."""
    steady_history = [50_000, 51_000, 49_500, 50_200, 49_900, 50_100]

    reason = detect_anomaly(steady_history, today_cents=0)

    assert reason is not None


def test_a_tiny_absolute_swing_on_a_near_zero_baseline_is_not_flagged() -> None:
    """A business doing a few dollars a day must not trip this on completely
    routine noise just because its trailing stddev happens to be tiny --
    MIN_ABSOLUTE_CENTS is the floor that prevents that."""
    near_zero_history = [100, 150, 120, 90, 110]

    assert detect_anomaly(near_zero_history, today_cents=4_000) is None


def test_a_large_absolute_swing_on_a_near_zero_baseline_is_still_flagged() -> None:
    near_zero_history = [100, 150, 120, 90, 110]

    reason = detect_anomaly(near_zero_history, today_cents=1_000_000)

    assert reason is not None
