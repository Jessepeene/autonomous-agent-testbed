"""Scheduler helpers that need no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

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


# --- the Discovery off switch -----------------------------------------------


class _ExplodingSession:
    """Any real use of the session means the guard did not return early."""

    async def execute(self, *_: object, **__: object) -> None:
        raise AssertionError("scheduler touched the database despite discovery being off")


def _ctx(*, discovery_enabled: bool) -> Any:
    from factory.settings import EngineName, Settings

    settings = Settings(discovery_enabled=discovery_enabled)
    return SimpleNamespace(
        settings=settings,
        enabled_engines=set(EngineName),
        is_enabled=lambda engine: True,
    )


async def test_disabling_discovery_stops_new_cycles_being_scheduled() -> None:
    """The switch exists because the previous way of doing this was a very large
    `discovery_interval_seconds`, which works only by arithmetic accident -- the
    idempotency bucket `now // interval` stops advancing -- reads as a typo, and
    still lets one cycle through on the first tick after the change."""
    from factory.supervisor.scheduler import Scheduler

    scheduler = Scheduler(_ctx(discovery_enabled=False), graph=None)
    await scheduler._schedule_discovery(_ExplodingSession())


async def test_the_switch_does_not_touch_the_other_departments() -> None:
    """The blunt alternative -- unsetting GEMINI_API_KEY -- disables Compliance and
    Marketing alongside Discovery, which is exactly wrong when the point is to watch
    one existing product move through them."""
    from factory.settings import EngineName

    ctx = _ctx(discovery_enabled=False)
    for engine in (EngineName.COMPLIANCE, EngineName.MARKETING, EngineName.DEVELOPMENT):
        assert engine in ctx.enabled_engines
