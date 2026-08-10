"""`ScoredIdea.weighted_score()` — added alongside the untouched `.score` property so
every existing caller that never heard of the learning loop keeps behaving exactly as
before, while `discovery/graph.py` can pass tuned weights explicitly."""

from __future__ import annotations

from factory.engines.discovery.score import survivors
from factory.state.schemas import DiscriminatedIdeas, ScoredIdea

_URL = "https://example.test/thread"


def _idea(pain: float, pay: float, feasible: float, adoption: float) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="x" * 30,
        target_niche="Bookkeepers at small Dutch firms",
        source_url=_URL,
        monetization_hypothesis="y" * 30,
        pain_severity=pain,
        willingness_to_pay=pay,
        build_feasibility=feasible,
        adoption_ease=adoption,
    )


def test_score_uses_the_fixed_historical_weights() -> None:
    idea = _idea(1.0, 1.0, 1.0, 1.0)
    assert idea.score == 1.0  # 0.3 + 0.3 + 0.15 + 0.25


def test_weighted_score_with_default_weights_matches_score() -> None:
    idea = _idea(0.8, 0.6, 0.3, 0.5)
    assert idea.weighted_score(ScoredIdea.default_weights()) == idea.score


def test_weighted_score_with_custom_weights_differs_from_the_fixed_score() -> None:
    idea = _idea(1.0, 0.0, 0.0, 0.0)
    custom = {"pain_severity": 0.0, "willingness_to_pay": 1.0, "build_feasibility": 0.0}
    assert idea.weighted_score(custom) == 0.0
    assert idea.score == 0.3  # unaffected by the custom weights


def test_survivors_defaults_to_the_fixed_weights_when_none_are_given() -> None:
    idea = _idea(0.9, 0.9, 0.9, 0.9)  # score 0.9, clears the default 0.55 threshold
    result = DiscriminatedIdeas(ideas=[idea])
    assert survivors(result) == [idea]


def test_survivors_uses_supplied_weights_over_the_fixed_default() -> None:
    """A weight change can flip whether an idea survives, at the same threshold."""
    idea = _idea(pain=0.9, pay=0.0, feasible=0.0, adoption=0.0)
    result = DiscriminatedIdeas(ideas=[idea])

    # Default weights: 0.9*0.3 = 0.27, below the 0.55 threshold.
    assert survivors(result) == []

    # Custom weights putting everything on pain_severity: 0.9*1.0 = 0.9, survives.
    heavy_pain = {"pain_severity": 1.0, "willingness_to_pay": 0.0, "build_feasibility": 0.0}
    assert survivors(result, heavy_pain) == [idea]
