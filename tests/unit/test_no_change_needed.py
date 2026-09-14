""""I investigated and nothing should change" is an outcome, not a failure.

A repair agent was handed a brief claiming a landing page was missing two URLs. It
checked, found the claim false for one and forbidden by the product's own
acceptance criteria for the other, refused to evade a test assertion to satisfy it,
and documented all of that. In its own words:

    Made NO code change. Investigated the reviewer guidance and concluded it must
    not be actioned, because both halves are either already satisfied or directly
    forbidden by this product's own acceptance criteria.
    ...
    I rejected: adding a hidden/beacon-style element using a non-src attribute to
    dodge 'src="http' -- that is deliberately evading an assertion.

That was recorded as `repair_unusable: the agent changed nothing` and burned a heal
cycle, because usability was `bool(changed_files)`. A reasoned refusal and an agent
that tried and failed were the same thing to the system -- the same shape as
RESULT: BLOCKED being demanded by the rules and read by nothing.

A brief that turns out to be wrong is a finding. The loop should end there rather
than spend its remaining budget trying to make a false premise true.
"""

from __future__ import annotations

import pytest

from factory.agents.roles import ROLES, RoleName
from factory.agents.runtime import parse_reported_result
from factory.engines.development.repair import RepairOutcome


def test_the_verdict_is_in_the_vocabulary() -> None:
    assert parse_reported_result("RESULT: NOT_NEEDED the claim was false")[0] == "NOT_NEEDED"


def test_it_is_distinct_from_failing_to_fix() -> None:
    """NOT_FIXED means "I could not". NOT_NEEDED means "it should not be". Collapsing
    them is what made a correct refusal look like a failed attempt."""
    assert parse_reported_result("RESULT: NOT_FIXED")[0] == "NOT_FIXED"
    assert parse_reported_result("RESULT: NOT_NEEDED")[0] == "NOT_NEEDED"


def test_a_refusal_is_a_conclusion_even_though_nothing_changed() -> None:
    outcome = RepairOutcome(
        changed_files=set(), answer="the brief was wrong", summary="8 steps", no_change_needed=True
    )
    assert outcome.concluded
    # Still not "usable" -- nothing shipped, and `usable` means work happened.
    assert not outcome.usable
    assert outcome.rejected_because is None


def test_an_agent_that_did_nothing_at_all_is_still_a_failure() -> None:
    """The distinction has to survive the obvious abuse: claiming a brief is wrong
    is only credible from an agent that actually investigated."""
    outcome = RepairOutcome(
        changed_files=set(),
        answer="looks fine to me",
        summary="0 steps",
        rejected_because="the agent changed nothing",
    )
    assert not outcome.concluded
    assert not outcome.usable


def test_real_work_is_still_the_normal_path() -> None:
    outcome = RepairOutcome(changed_files={"app/main.py"}, answer="fixed", summary="6 steps")
    assert outcome.usable
    assert outcome.concluded


@pytest.mark.parametrize("role", [RoleName.WORKSPACE_SURGEON, RoleName.SEO_OPTIMIZER])
def test_the_repair_roles_are_told_the_option_exists(role: RoleName) -> None:
    """A verdict nobody is told they may give does not get given -- the same lesson
    as a tool nobody is told to call."""
    prompt = ROLES[role].system_prompt
    assert "NOT_NEEDED" in prompt
    assert "should NOT be made" in prompt
