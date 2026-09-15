"""Requirements: the step a human team never skips and this factory never had.

Nothing here ever wrote down what it was trying to build. The landing-page prompt
carries requirements, but generic ones -- the same form, beacon and SEO tags for
every product ever generated. What *this* page must do for *this* idea existed
nowhere, so everything downstream judged against nothing: Bug Council reviews
categories rather than intent, AUDITOR infers the goal by looking at the product,
and release was `if not deploy_blocking`.

The property these tests protect is the one that makes requirements more than a
longer checklist: **done and perfect are different states**. A flat list of
requirements is just another unbounded gate, which is exactly what the old deploy
gate was.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.engines.development.requirements import render_for_prompt
from factory.state.schemas import AcceptanceCriterion, WorkRequirements


def _criterion(text: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(criterion=text, why="it is why the page exists at all")


def _requirements(**overrides: object) -> WorkRequirements:
    base: dict[str, object] = {
        "goal": "Find out whether iOS engineering leads want a test sharding tool.",
        "must": [_criterion("A visitor can submit an email and sees a confirmation.")],
        "should": [_criterion("The page states how long setup takes.")],
        "out_of_scope": ["user accounts", "a working product behind the page"],
    }
    base.update(overrides)
    return WorkRequirements(**base)  # type: ignore[arg-type]


def test_must_cannot_be_empty() -> None:
    """Requirements with nothing non-negotiable in them are decoration. If a build
    cannot fail them, no downstream judgement that cites them means anything."""
    with pytest.raises(ValidationError):
        _requirements(must=[])


def test_should_and_out_of_scope_may_be_empty() -> None:
    """Not everything has nice-to-haves or explicit exclusions, and demanding them
    would produce invented ones."""
    assert _requirements(should=[], out_of_scope=[]).must


class TestRendering:
    def test_must_and_should_are_visibly_different(self) -> None:
        """The whole point. An employee reading this has to be able to tell what
        blocks a release from what merely improves it -- that distinction is what
        lets a product be done without being perfect."""
        rendered = render_for_prompt(_requirements())
        assert "MUST (not done without these)" in rendered
        assert "SHOULD (valuable, does not block a release)" in rendered

    def test_out_of_scope_says_not_to_flag_them(self) -> None:
        """Named so a reviewer stops reporting the absence of things nobody meant to
        build, and a repair agent stops building them."""
        rendered = render_for_prompt(_requirements())
        assert "OUT OF SCOPE" in rendered
        assert "do not add or flag" in rendered
        assert "user accounts" in rendered

    def test_the_goal_leads(self) -> None:
        """What the work is *for* comes before the checklist: a criterion read
        without its purpose is a rule, and rules cannot be weighed."""
        assert render_for_prompt(_requirements()).startswith("What this must achieve:")

    def test_each_criterion_carries_its_reason(self) -> None:
        """`why` exists so a reviewer can tell a near-miss from a real failure. A
        criterion stripped of its reason can only be ticked, never judged."""
        rendered = render_for_prompt(_requirements())
        assert "(it is why the page exists at all)" in rendered

    def test_a_requirements_block_omits_sections_it_does_not_have(self) -> None:
        rendered = render_for_prompt(_requirements(should=[], out_of_scope=[]))
        assert "SHOULD" not in rendered
        assert "OUT OF SCOPE" not in rendered


def test_criteria_must_be_substantial_enough_to_check() -> None:
    """"Fast" and "good UX" are the failure mode: a reviewer cannot fail them
    honestly, which makes every judgement citing them unfalsifiable. The length
    floor does not guarantee checkability, but it does stop one-word wishes."""
    with pytest.raises(ValidationError):
        AcceptanceCriterion(criterion="fast", why="performance matters to users")


def test_a_products_own_guidelines_replace_the_house_style() -> None:
    """The half of the loop that was missing.

    Design could write a standard for one specific product and store it correctly;
    `HOUSE_STYLE` was what the builder and the reviewer actually read, so the page it
    was written for was built to the general house style instead. Observed the day
    the department produced its first real guideline.

    Replace rather than stack, because the two are different in kind. `HOUSE_STYLE`
    is instructional -- "choose 4-6 named hex values and state them before building".
    A product's guidelines are the decided answer. They do not contradict, because
    the specific one was written *from* the generic one.
    """
    from factory.engines.development.requirements import HOUSE_STYLE, effective_design
    from factory.state.schemas import DesignGuidelines

    decided = DesignGuidelines(
        direction="Borrow the language of build logs: left-aligned, monospace timings.",
        palette=["#E74C3C", "#2C3E50"],
        typography="IBM Plex Serif display, IBM Plex Sans body, both from Google Fonts.",
        must_hold=["The palette centers on #E74C3C, the build-failed state"],
    )

    assert effective_design(decided) is decided
    assert effective_design(None) is HOUSE_STYLE


def test_the_builder_and_the_reviewer_cannot_hold_different_standards() -> None:
    """A page built to one standard and judged against another fails unactionably.

    Both resolve through `effective_design`, so this is really asserting that there
    is only one resolution point -- the property `render_design_for_prompt`'s
    docstring demands and that nothing enforced.
    """
    import inspect

    from factory.engines.development import gaps
    from factory.engines.development.scaffold import generator

    builder = inspect.signature(generator.generate_scaffold).parameters["design"]
    reviewer = inspect.signature(gaps.find_requirement_gaps).parameters["design"]

    # Same default, so an un-wired caller cannot silently create a mismatch.
    assert builder.default is reviewer.default
    assert builder.annotation == reviewer.annotation


def test_the_landing_page_gets_guidelines_only_when_they_were_decided() -> None:
    """Deliberately not defaulted to the house style, unlike the other two.

    `LANDING_PAGE_SYSTEM` already carries the general direction in prose, so passing
    `HOUSE_STYLE` here would be a second copy of guidance the model already has --
    tokens spent to say the same thing twice. Only a real decision earns the space.
    """
    import inspect

    from factory.engines.development.scaffold import generator

    param = inspect.signature(generator.generate_landing_page).parameters["design"]
    assert param.default is None
