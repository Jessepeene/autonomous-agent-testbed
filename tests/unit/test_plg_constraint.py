"""The self-serve / PLG admission gate.

Distribution, not code generation, is the bottleneck this constraint exists for: an
idea that needs a human to sell or onboard it does not fit a factory with no sales
team, however strong its raw score. The gate runs *after* the discriminator scores an
idea and is purely deterministic — the model self-reports the three inputs, code makes
the binding decision, exactly like the LIA's `passed` field.
"""

from __future__ import annotations

from typing import Any

from factory.engines.discovery.score import (
    PLG_PRICE_CEILING_USD,
    passes_plg_constraint,
    survivors,
)
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


# --- passes_plg_constraint ----------------------------------------------------


def test_self_serve_idea_passes() -> None:
    idea = _idea(
        requires_human_sales=False, requires_human_onboarding=False, self_serve_price_usd=49.0
    )
    assert passes_plg_constraint(idea) is True


def test_requires_human_sales_is_disqualifying() -> None:
    idea = _idea(requires_human_sales=True, self_serve_price_usd=49.0)
    assert passes_plg_constraint(idea) is False


def test_requires_human_onboarding_is_disqualifying() -> None:
    idea = _idea(requires_human_onboarding=True, self_serve_price_usd=49.0)
    assert passes_plg_constraint(idea) is False


def test_price_above_ceiling_is_disqualifying() -> None:
    idea = _idea(self_serve_price_usd=PLG_PRICE_CEILING_USD + 0.01)
    assert passes_plg_constraint(idea) is False


def test_price_at_the_ceiling_is_allowed() -> None:
    idea = _idea(self_serve_price_usd=PLG_PRICE_CEILING_USD)
    assert passes_plg_constraint(idea) is True


def test_existing_callers_default_to_passing() -> None:
    """A `ScoredIdea` built the way every pre-existing test and caller builds one —
    no PLG fields specified — must not suddenly fail this new gate."""
    idea = _idea()
    assert passes_plg_constraint(idea) is True


# --- survivors() applies the gate after scoring -------------------------------


def test_survivors_drops_a_high_scoring_idea_that_fails_plg() -> None:
    high_score_but_needs_sales = _idea(
        pain_severity=1.0,
        willingness_to_pay=1.0,
        build_feasibility=1.0,
        requires_human_sales=True,
    )
    self_serve = _idea(problem_statement="A different, unrelated pain point entirely.")
    result = DiscriminatedIdeas(ideas=[high_score_but_needs_sales, self_serve])

    kept = survivors(result)

    assert high_score_but_needs_sales not in kept
    assert self_serve in kept
