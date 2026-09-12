"""Deterministic anomaly detection on real ledger data (Tier 0 of the
system-wide verification plan).

Finance is the one engine with zero LLM involvement in any real financial
number, and that is correct and should stay that way (see `graph.py`'s own
docstring). The founder's-instinct equivalent here is not "add a model to
judge this" -- it's noticing an implausible day-over-day change the way an
owner checking their own bank balance would raise an eyebrow at a number
that doesn't fit the recent pattern, before assuming the bookkeeping is
simply correct. Pure arithmetic, same spirit as `supervisor/audit.py`'s
existing invariant sweeps, deliberately kept model-free to match the rest of
this engine.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime

#: Below this many days of history, there is no real baseline to compare
#: against yet -- a business a few days old should not have its first real
#: revenue day flagged as "anomalous" relative to nothing.
MIN_DAYS_FOR_BASELINE = 5

#: How many standard deviations from the trailing mean counts as worth a
#: human's attention. Wide on purpose: this is meant to catch a genuine
#: 10x-style swing, not ordinary day-to-day noise.
STDDEV_MULTIPLIER = 4.0

#: A floor under the statistical threshold, in cents -- a business doing a
#: few dollars a day would otherwise trip this check on completely routine
#: swings, since a tiny trailing stddev makes even a $20 move look like many
#: multiples of it.
MIN_ABSOLUTE_CENTS = 5_000


def daily_totals(rows: list[tuple[datetime, int]]) -> dict[date, int]:
    """Buckets a raw `(created_at, amount_cents)` series into per-day sums."""
    totals: dict[date, int] = defaultdict(int)
    for created_at, amount_cents in rows:
        totals[created_at.date()] += amount_cents
    return totals


def detect_anomaly(history_cents: list[int], today_cents: int) -> str | None:
    """``history_cents`` is the trailing window's daily totals, NOT including
    today, in any order. Returns a human-readable reason if today's total is
    a statistical outlier relative to that window, or ``None`` for an
    ordinary day -- including "not enough history yet to have an opinion",
    which fails toward silence rather than a false alarm on a young dataset.
    """
    if len(history_cents) < MIN_DAYS_FOR_BASELINE:
        return None
    mean = statistics.mean(history_cents)
    stddev = statistics.pstdev(history_cents)
    deviation = abs(today_cents - mean)
    threshold = max(stddev * STDDEV_MULTIPLIER, MIN_ABSOLUTE_CENTS)
    if deviation <= threshold:
        return None
    return (
        f"today's total (${today_cents / 100:,.2f}) deviates from the trailing "
        f"{len(history_cents)}-day average (${mean / 100:,.2f}) by "
        f"${deviation / 100:,.2f} -- more than {STDDEV_MULTIPLIER:g}x the trailing "
        f"standard deviation (${stddev / 100:,.2f})"
    )
