"""Releasing as a judgement, with rails that do not move.

This replaces `if not deploy_blocking: ship`. That line was the highest-judgement
moment in the company -- is this good enough to put in front of a customer --
expressed as a boolean, and it is what made the build a retry loop rather than a
process: every transition was automatic, so the system had one move and made it
until a counter ran out.

The two properties worth protecting pull against each other, which is why both are
pinned here: the decision must be *real* (four genuine options, including the two
that say more patching will not help), and it must never be able to wave through a
finding the rails forbid.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.state.schemas import (
    BLOCKING_SEVERITIES,
    STRICT_CATEGORIES,
    ReleaseDecision,
)


def _decision(**overrides: object) -> ReleaseDecision:
    base: dict[str, object] = {
        "decision": "ship",
        "rationale": "It meets every must-have; the remaining findings are cosmetic.",
    }
    base.update(overrides)
    return ReleaseDecision(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("choice", ["ship", "fix_first", "rethink", "abandon"])
def test_all_four_options_are_expressible(choice: str) -> None:
    """A system with one move has a retry loop. rethink and abandon are the two the
    old gate could not say at all -- "the approach is wrong" and "this is not worth
    more effort" are conclusions no amount of repair budget ever reaches."""
    assert _decision(decision=choice).decision == choice


def test_an_invented_option_is_refused() -> None:
    with pytest.raises(ValidationError):
        _decision(decision="ship_it_probably")


def test_a_decision_must_carry_reasoning() -> None:
    """The rationale is read by a human who has seen none of this. A decision
    without one is the boolean again, wearing a different type."""
    with pytest.raises(ValidationError):
        _decision(rationale="fine")


def test_shipping_with_known_issues_is_sayable() -> None:
    """The state the old gate could not express. "No blocking findings" is
    unbounded -- there is always another finding -- so `done` was unreachable.
    Accepting named issues deliberately is what makes it reachable."""
    from factory.state.schemas import AcceptedIssue

    decision = _decision(
        accepted_issues=[
            AcceptedIssue(
                issue_ref="a1b2c3d4", why="A stale footer year costs no visitor anything."
            ),
            AcceptedIssue(
                issue_ref="e5f6a7b8", why="A spinner is polish; the form works without it."
            ),
        ]
    )
    assert decision.decision == "ship"
    assert len(decision.accepted_issues) == 2
    # References, not prose. Free text could not reach the row it was about, so the
    # acceptance was logged and the same findings came back blocking on the next
    # pass -- observed live before this shape changed.
    assert decision.accepted_issues[0].issue_ref == "a1b2c3d4"


def test_fix_first_carries_what_specifically() -> None:
    """The triage step nothing previously did. "Fix all findings" is how a product
    never ships; naming the fewest changes that would make it shippable is the
    judgement."""
    decision = _decision(
        decision="fix_first",
        fix_first=["checkout returns 422 on submit"],
        rationale="One thing stands between this and a release: the payment path.",
    )
    assert decision.fix_first == ["checkout returns 422 on submit"]


def test_unmet_requirements_are_named_not_summarised() -> None:
    """ "It does not meet the requirements" is unactionable. Which ones is the
    difference between a repair brief and a complaint."""
    decision = _decision(
        decision="fix_first",
        unmet_must=["A visitor can submit an email and sees a confirmation."],
        rationale="The interest form is the entire point of the page and it fails.",
    )
    assert decision.unmet_must


class TestTheRailsDoNotMove:
    """`decide_release` overrules a `ship` when the council raised a finding at a
    severity that cannot ship. The judgement is still recorded as made -- what it
    chose and why is exactly the evidence worth keeping when it is overruled -- but
    signing off a severe finding is not something an employee may do."""

    def test_the_blocking_set_is_still_severity_driven(self) -> None:
        assert BLOCKING_SEVERITIES == {"critical", "high"}

    def test_leaky_categories_are_still_stricter(self) -> None:
        assert STRICT_CATEGORIES == {"security", "secrets", "privilege"}

    def test_an_overruled_ship_becomes_fix_first_not_a_silent_deploy(self) -> None:
        """Modelled the same way `decide_release` does it, so the shape of the
        override is pinned even though the function itself needs a live provider."""
        judged = _decision(
            decision="ship",
            rationale="Every must-have is met and the rest are cosmetic nits.",
        )
        overruled = judged.model_copy(
            update={
                "decision": "fix_first",
                "rationale": "Overruled by the release rails: " + judged.rationale,
                "fix_first": ["unbounded FAILED_LOGIN_LOG growth"],
            }
        )
        assert overruled.decision == "fix_first"
        assert "Overruled by the release rails" in overruled.rationale
        assert overruled.fix_first


@pytest.mark.asyncio
async def test_cost_of_delay_facts_degrade_rather_than_crash_the_judgement() -> None:
    """`decide_release`'s own docstring already promises this for the model call:
    "failing to reach a judgement must not become an outage." This function is the
    one place that promise was not yet kept.

    Caught live: a unit test exercising a fully deterministic slice of the build
    pipeline passes `session=None`, on the reasonable assumption that a path with no
    model call and no requirements needs no database either -- true until this
    function added one. `session=None` here stands in for that same assumption
    failing; a real session that has simply lost its connection degrades the same
    way, through the same `except`.
    """
    from factory.engines.development.release import _render_cost_of_delay_facts

    facts = await _render_cost_of_delay_facts(None, "prod-1", heal_attempts=2)  # type: ignore[arg-type]
    assert facts == []
