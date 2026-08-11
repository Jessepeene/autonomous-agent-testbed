"""`WeightsCandidate`'s sum-to-one guard.

Regression coverage for a bug found in the 2026-08-10 full-codebase audit:
`ScoredIdea.weighted_score` is a plain weighted sum with no normalisation, and
`SCORE_THRESHOLD` is a fixed constant calibrated against weights summing to 1.0.
Bounding each weight to [0, 1] independently let a proposed set like
{0.5, 0.5, 0.5, 0.5} (sum 2.0) roughly double every idea's score -- silently
turning the quality filter into a near no-op, with no way for the trial mechanism
to detect or reverse it once such a trial statistically "won".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.learning.maintenance import WeightsCandidate


def _weights(**overrides: float) -> dict[str, float]:
    base = {
        "pain_severity": 0.25,
        "willingness_to_pay": 0.25,
        "build_feasibility": 0.25,
        "adoption_ease": 0.25,
    }
    base.update(overrides)
    return base


def test_weights_summing_to_one_are_accepted() -> None:
    candidate = WeightsCandidate(
        no_change_recommended=False,
        rationale="Emphasise willingness to pay a bit more than before.",
        **_weights(
            pain_severity=0.3, willingness_to_pay=0.3, build_feasibility=0.2, adoption_ease=0.2
        ),
    )
    assert candidate.willingness_to_pay == 0.3


def test_weights_summing_to_double_are_rejected() -> None:
    with pytest.raises(ValidationError, match="sum to"):
        WeightsCandidate(
            no_change_recommended=False,
            rationale="This would roughly double every idea's score.",
            pain_severity=0.5,
            willingness_to_pay=0.5,
            build_feasibility=0.5,
            adoption_ease=0.5,
        )


def test_weights_summing_near_zero_are_rejected() -> None:
    with pytest.raises(ValidationError, match="sum to"):
        WeightsCandidate(
            no_change_recommended=False,
            rationale="This would make almost every idea score near zero.",
            pain_severity=0.05,
            willingness_to_pay=0.05,
            build_feasibility=0.0,
            adoption_ease=0.0,
        )


def test_small_floating_point_slop_is_tolerated() -> None:
    candidate = WeightsCandidate(
        no_change_recommended=False,
        rationale="Effectively unchanged from the default weights.",
        pain_severity=0.26,
        willingness_to_pay=0.25,
        build_feasibility=0.25,
        adoption_ease=0.25,
    )
    assert candidate.pain_severity == 0.26


def test_no_change_recommended_skips_the_sum_check() -> None:
    """The numeric fields are unused placeholder defaults when no change is
    recommended -- must not block on their sum."""
    candidate = WeightsCandidate(
        no_change_recommended=True,
        rationale="Current weights are performing within the expected range.",
        pain_severity=0.0,
        willingness_to_pay=0.0,
        build_feasibility=0.0,
        adoption_ease=0.0,
    )
    assert candidate.no_change_recommended is True
