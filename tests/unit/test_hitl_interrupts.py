from __future__ import annotations

from datetime import UTC, datetime, timedelta

from factory.hitl.interrupts import _age


def test_age_under_a_day_is_reported_in_hours() -> None:
    created_at = datetime.now(UTC) - timedelta(hours=5)
    assert _age(created_at) == "5h"


def test_age_at_the_24_hour_boundary_switches_to_days() -> None:
    created_at = datetime.now(UTC) - timedelta(hours=24)
    assert _age(created_at) == "1d"


def test_age_just_under_the_boundary_stays_in_hours() -> None:
    created_at = datetime.now(UTC) - timedelta(hours=23, minutes=59)
    assert _age(created_at) == "23h"


def test_age_several_days_out() -> None:
    created_at = datetime.now(UTC) - timedelta(days=3, hours=2)
    assert _age(created_at) == "3d"
