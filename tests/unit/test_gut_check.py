"""The discriminator's holistic veto (Tier 0 of the system-wide verification
plan) — an idea can score adequately on every one of the five isolated
scored dimensions (`score.py::DISCRIMINATOR_SYSTEM`) while being, taken as a
whole, obviously not a real business. `gut_check_passed` is a sixth,
independent signal the model self-reports; the gate itself is deterministic,
exactly like `passes_plg_constraint`.
"""

from __future__ import annotations

from typing import Any

from factory.engines.discovery.score import passes_gut_check, survivors
from factory.state.schemas import DiscriminatedIdeas, ScoredIdea


def _idea(**overrides: Any) -> ScoredIdea:
    base: dict[str, Any] = {
        "problem_statement": "Bookkeepers reconcile invoices by hand every week.",
        "target_niche": "Bookkeepers at small Dutch accounting firms",
        "source_url": "https://example.test/thread",
        "monetization_hypothesis": "EUR 49/month per seat.",
        "pain_severity": 0.8,
        "willingness_to_pay": 0.7,
        "build_feasibility": 0.6,
        "adoption_ease": 0.8,
    }
    base.update(overrides)
    return ScoredIdea(**base)


# --- passes_gut_check -----------------------------------------------------------


def test_a_passed_gut_check_is_allowed() -> None:
    idea = _idea(gut_check_passed=True)
    assert passes_gut_check(idea) is True


def test_a_failed_gut_check_is_disqualifying() -> None:
    idea = _idea(gut_check_passed=False, gut_check_reasoning="Not a real business.")
    assert passes_gut_check(idea) is False


def test_existing_callers_default_to_passing() -> None:
    """A `ScoredIdea` built the way every pre-existing test and caller builds
    one -- no gut-check fields specified -- must not suddenly fail this new
    gate. Also protects against a stale served DISCRIMINATOR_SYSTEM prompt
    (`learning/prompts.py::get_prompt`) that predates this field: a missing
    gut check must do nothing, never silently veto every idea."""
    idea = _idea()
    assert passes_gut_check(idea) is True


# --- survivors() applies the gate after scoring, independent of weighted_score --


def test_survivors_drops_a_high_scoring_idea_that_fails_gut_check() -> None:
    high_score_but_gut_check_failed = _idea(
        pain_severity=1.0,
        willingness_to_pay=1.0,
        build_feasibility=1.0,
        gut_check_passed=False,
        gut_check_reasoning="This is a thin wrapper around an existing well-known product.",
    )
    sane_idea = _idea(problem_statement="A different, unrelated pain point entirely.")
    result = DiscriminatedIdeas(ideas=[high_score_but_gut_check_failed, sane_idea])

    kept = survivors(result)

    assert high_score_but_gut_check_failed not in kept
    assert sane_idea in kept
