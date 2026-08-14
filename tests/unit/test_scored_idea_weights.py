"""`ScoredIdea.weighted_score()` — added alongside the untouched `.score` property so
every existing caller that never heard of the learning loop keeps behaving exactly as
before, while `discovery/graph.py` can pass tuned weights explicitly.

Phase 4 added a fifth weighted dimension (`defensibility`, defaulted to 0.0) --
every arithmetic assertion below accounts for it explicitly, since an idea built
without setting it no longer scores as if only four dimensions existed.
"""

from __future__ import annotations

from factory.engines.discovery.score import survivors
from factory.state.schemas import (
    DiscriminatedIdeas,
    PlatformDependency,
    ReplacementMode,
    ScoredIdea,
)

_URL = "https://example.test/thread"


def _idea(
    pain: float, pay: float, feasible: float, adoption: float, defensibility: float = 0.0
) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="x" * 30,
        target_niche="Bookkeepers at small Dutch firms",
        source_url=_URL,
        monetization_hypothesis="y" * 30,
        pain_severity=pain,
        willingness_to_pay=pay,
        build_feasibility=feasible,
        adoption_ease=adoption,
        defensibility=defensibility,
    )


def test_score_uses_the_fixed_historical_weights() -> None:
    idea = _idea(1.0, 1.0, 1.0, 1.0, defensibility=1.0)
    assert idea.score == 1.0  # 0.25 + 0.25 + 0.12 + 0.20 + 0.18


def test_weighted_score_with_default_weights_matches_score() -> None:
    idea = _idea(0.8, 0.6, 0.3, 0.5, defensibility=0.4)
    assert idea.weighted_score(ScoredIdea.default_weights()) == idea.score


def test_weighted_score_with_custom_weights_differs_from_the_fixed_score() -> None:
    idea = _idea(1.0, 0.0, 0.0, 0.0)
    custom = {"pain_severity": 0.0, "willingness_to_pay": 1.0, "build_feasibility": 0.0}
    assert idea.weighted_score(custom) == 0.0
    assert idea.score == 0.25  # 1.0 * pain_severity's 0.25 weight, unaffected by custom weights


def test_survivors_defaults_to_the_fixed_weights_when_none_are_given() -> None:
    idea = _idea(0.9, 0.9, 0.9, 0.9, defensibility=0.9)  # score 0.9, clears the 0.55 threshold
    result = DiscriminatedIdeas(ideas=[idea])
    assert survivors(result) == [idea]


def test_survivors_uses_supplied_weights_over_the_fixed_default() -> None:
    """A weight change can flip whether an idea survives, at the same threshold."""
    idea = _idea(pain=0.9, pay=0.0, feasible=0.0, adoption=0.0)
    result = DiscriminatedIdeas(ideas=[idea])

    # Default weights: 0.9*0.25 = 0.225, below the 0.55 threshold.
    assert survivors(result) == []

    # Custom weights putting everything on pain_severity: 0.9*1.0 = 0.9, survives.
    heavy_pain = {"pain_severity": 1.0, "willingness_to_pay": 0.0, "build_feasibility": 0.0}
    assert survivors(result, heavy_pain) == [idea]


# --- Phase 4: the defensibility dimension --------------------------------------


def test_defensibility_defaults_to_zero_and_contributes_nothing_unset() -> None:
    """A discriminator response built before Phase 4 existed (or a stale served
    prompt that never asks for it) must not crash -- it just scores as if
    defensibility were the worst possible answer, which is the fail-safe
    direction: it makes survival *harder*, never silently easier."""
    idea = _idea(1.0, 1.0, 1.0, 1.0)  # defensibility left at its 0.0 default
    assert idea.defensibility == 0.0
    assert idea.score == 0.82  # 1.0 - the 0.18 defensibility weight


def test_defensibility_moves_the_score_by_exactly_its_weight() -> None:
    low = _idea(0.5, 0.5, 0.5, 0.5, defensibility=0.0)
    high = _idea(0.5, 0.5, 0.5, 0.5, defensibility=1.0)
    assert round(high.score - low.score, 4) == 0.18


def test_score_dimensions_are_exactly_the_five_weighted_fields() -> None:
    assert ScoredIdea.SCORE_DIMENSIONS == (
        "pain_severity",
        "willingness_to_pay",
        "build_feasibility",
        "adoption_ease",
        "defensibility",
    )


def test_default_weights_sum_to_one() -> None:
    assert round(sum(ScoredIdea.default_weights().values()), 6) == 1.0
    assert set(ScoredIdea.default_weights()) == set(ScoredIdea.SCORE_DIMENSIONS)


def test_platform_dependency_and_replacement_mode_default_to_unknown() -> None:
    """Informational fields, never gated or weighted in Phase 4 -- defaulted so a
    stale served prompt that doesn't ask for them still validates."""
    idea = _idea(0.5, 0.5, 0.5, 0.5)
    assert idea.platform_dependency is PlatformDependency.UNKNOWN
    assert idea.replacement_mode is ReplacementMode.UNKNOWN


def test_defensibility_reasoning_defaults_to_empty_string() -> None:
    idea = _idea(0.5, 0.5, 0.5, 0.5)
    assert idea.defensibility_reasoning == ""


def test_platform_dependency_and_replacement_mode_are_never_weighted() -> None:
    """Setting either to its most extreme member must not move the score --
    they're deliberately absent from SCORE_DIMENSIONS."""
    baseline = _idea(0.5, 0.5, 0.5, 0.5, defensibility=0.5)
    extreme = ScoredIdea(
        **{
            **baseline.model_dump(),
            "platform_dependency": PlatformDependency.REQUIRED,
            "replacement_mode": ReplacementMode.REPLACES,
        }
    )
    assert extreme.score == baseline.score
