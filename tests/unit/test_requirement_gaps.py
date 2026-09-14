"""The one comparison: does this build do what it was for?

Everything that judged a build before this judged it against something else -- the
codegen contract against a hardcoded list of environment facts, Bug Council against
six fixed categories. Neither can read the requirements, and those two were the ones
that could veto a release.

The worked example these tests exist for: a landing page passed every gate --
contract green, tests passing, all six review categories quiet -- while running an
analytics beacon and telling visitors in its own copy "No spam, no newsletter, no
tracking scripts." That is a gap against intent, and nothing reviewing categories
can see it.

The property that matters most is the one in the name: **gaps, not findings**. A
gap is the distance to a stated standard, so there are finitely many and fixing one
removes it. Findings are unbounded -- there is always another unbounded cache -- and
a gate defined as "no outstanding findings" can never be satisfied, which is exactly
how 19 issues became 28 in a single cycle.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.engines.development.gaps import _TIER_SEVERITY, GAP_SYSTEM
from factory.state.schemas import GapReport, RequirementGap


def _gap(tier: str = "must", **overrides: object) -> RequirementGap:
    base: dict[str, object] = {
        "criterion": "The page states the price of $199 per month.",
        "tier": tier,
        "evidence": "No price appears anywhere in public/index.html.",
    }
    base.update(overrides)
    return RequirementGap(**base)  # type: ignore[arg-type]


def test_an_unmet_must_blocks_and_an_unmet_should_does_not() -> None:
    """The distinction that lets a product be done without being perfect. A flat
    list of unmet criteria would just be another unbounded gate."""
    assert _TIER_SEVERITY["must"] == "high"
    assert _TIER_SEVERITY["should"] == "low"


def test_a_gap_must_carry_evidence() -> None:
    """A gap asserted without evidence is an opinion. The entire point of comparing
    against a written standard is that the answer is checkable."""
    with pytest.raises(ValidationError):
        _gap(evidence="nope")


def test_met_criteria_are_reported_not_just_gaps() -> None:
    """So "done" is a positive statement backed by evidence rather than the absence
    of complaints -- and so a reviewer that looked at nothing is distinguishable from
    one that checked and found the product sound."""
    report = GapReport(gaps=[], met=["The page states the price of $199 per month."])
    assert report.met
    assert not report.gaps


def test_undetermined_is_a_third_answer_not_a_pass() -> None:
    """A criterion nobody could check has not passed. Folding it into `met` is the
    instrumentation failure this system keeps hitting -- claiming something is fine
    because nothing looked wrong."""
    report = GapReport(
        gaps=[],
        met=[],
        undetermined=["A visitor can submit an email and sees a confirmation."],
    )
    assert report.undetermined
    assert not report.met


def test_an_empty_report_is_valid_and_means_done() -> None:
    """No gaps is a reachable state. That is the whole difference from "no
    outstanding findings", which is not."""
    assert GapReport().gaps == []


class TestTheBriefKeepsItToTheComparison:
    """The failure mode to guard against is this becoming a seventh code reviewer --
    a list of everything wrong is precisely what stops products shipping."""

    def test_it_is_told_not_to_review_the_code(self) -> None:
        assert "You are not reviewing the code" in GAP_SYSTEM
        assert "other reviewers do" in GAP_SYSTEM

    def test_it_is_pointed_at_say_one_thing_do_another(self) -> None:
        """The defect class no category-based review can see, and the one that
        started all of this: a product promising what it does not do."""
        assert "says* one thing and *does* another" in GAP_SYSTEM

    def test_building_something_out_of_scope_is_also_a_gap(self) -> None:
        """A checkout on a page whose criteria forbid payment is as wrong as a
        missing requirement -- and that one cost 25 days."""
        assert "out of scope" in GAP_SYSTEM

    def test_guessing_is_refused_in_favour_of_undetermined(self) -> None:
        assert "undetermined" in GAP_SYSTEM
        assert "rather than guessing" in GAP_SYSTEM
