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

from factory.engines.development.requirements import (
    render_design_for_prompt,
    render_for_prompt,
)
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
        must_line = next(line for line in rendered.splitlines() if line.startswith("MUST"))
        should_line = next(line for line in rendered.splitlines() if line.startswith("SHOULD"))
        assert must_line != should_line
        assert "not done" in must_line
        assert "never blocking" in should_line

    def test_out_of_scope_says_a_departure_is_presence_not_absence(self) -> None:
        """Named so a reviewer stops reporting the absence of things nobody meant to
        build, and a repair agent stops building them.

        Strengthened after a live run filed `MUST not met: OUT OF SCOPE: A pricing
        page or price display` -- a thing deliberately not built, reported as missing,
        blocking a release it could never stop blocking. Saying "do not flag these"
        was not enough, because the reviewer had no other way to report the one case
        where an out-of-scope item genuinely is a departure: when it got built.
        """
        rendered = render_for_prompt(_requirements())
        section = next(line for line in rendered.splitlines() if line.startswith("OUT OF SCOPE"))
        assert "never be reported as unmet" in section
        assert "presence" in section
        assert "user accounts" in rendered

    def test_the_goal_leads_the_criteria(self) -> None:
        """What the work is *for* comes before the checklist: a criterion read
        without its purpose is a rule, and rules cannot be weighed."""
        rendered = render_for_prompt(_requirements())
        assert rendered.index("What this must achieve:") < rendered.index("MUST")

    def test_standing_policy_is_marked_as_not_being_criteria(self) -> None:
        """The failure this is here for, from a live run: a `PAGE_POLICY` line was
        reported as an unmet MUST -- a sentence explaining why a provisional canonical
        URL is correct, filed as a defect. It was graded because it arrived in the
        same undifferentiated block as the criteria, under a heading calling all of it
        "the acceptance criteria this product was built to meet"."""
        rendered = render_for_prompt(_requirements())
        preamble = rendered[rendered.index("not part of the criteria") :]
        assert "standing policy" in preamble
        assert "override any criterion" in preamble
        # The two properties that stop it being graded: a product cannot leave one
        # unmet, and it is therefore never a thing to report.
        assert "unmet" in preamble
        assert "report as a gap" in preamble

    def test_a_block_with_no_policy_still_renders(self) -> None:
        """`policy` is a parameter because standing policy is not a landing-page
        idea -- every department judging work against criteria has rules above them.
        A department without one yet must not get a dangling empty section."""
        rendered = render_for_prompt(_requirements(), policy="")
        assert "standing policy" not in rendered.lower()
        assert "MUST" in rendered

    def test_everything_rendered_can_be_placed_back_against_the_standard(self) -> None:
        """The invariant that stops the grounding check becoming a mute button.

        `render_for_prompt` hands a worker the standard; `standard_catalogue` is what
        its report is placed against. Anything in the first but not the second grounds
        nowhere, and a real blocking gap gets quietly downgraded to an observation.
        Found while building it: design `must_hold` is graded by the same pass that
        grades `must`, and leaving it out of the catalogue would have silenced exactly
        the defect a human had to catch.
        """
        from factory.engines.development.requirements import HOUSE_STYLE, standard_catalogue

        requirements = _requirements()
        rendered = render_for_prompt(requirements) + render_design_for_prompt(HOUSE_STYLE)
        catalogue = standard_catalogue(requirements, design=HOUSE_STYLE)

        for group, entries in catalogue.items():
            for entry in entries:
                assert entry in rendered, f"{group} entry is placeable but never shown: {entry!r}"

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
    """ "Fast" and "good UX" are the failure mode: a reviewer cannot fail them
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


def test_a_validation_page_is_never_regenerated_as_an_mvp() -> None:
    """The bug that turned a pitch page into a SaaS backend.

    `_patch`'s full-regeneration branch called `generate_scaffold` unconditionally,
    and that prompt mandates a FastAPI backend, token auth with password hashing and
    Stripe Checkout. So any full repair of a *validation* page -- whose whole job is
    to collect one email address -- rebuilt it as a complete MVP. The agent's own
    note on the last one reads: "Repaired the landing page and added the complete
    FastAPI MVP... token auth with scrypt password hashing... and Stripe Checkout."

    The same bug was fixed once already on the other side. `_verify_and_ship`'s
    `is_validation` comment ends: "The product became an MVP because this gate kept
    insisting it was one." The gate learned to read `build_kind`; the generator did
    not, so it survived in its other half.
    """
    import inspect

    from factory.engines.development import graph

    source = inspect.getsource(graph._patch)
    regeneration = source.split("existing = read_workspace")[1]

    assert "get_build_kind" in regeneration, "the regeneration must know what it is rebuilding"
    assert "generate_landing_page" in regeneration, "a validation page regenerates as one"


def test_every_builder_is_shown_the_standard_it_will_be_judged_against() -> None:
    """A regeneration that cannot see the criteria cannot meet them.

    `generate_landing_page` has taken `requirements` since criteria existed;
    `generate_scaffold` did not. So a release decision reported "five of the six
    MUSTs are completely unmet" for the honest reason that nobody had told the
    builder what they were.
    """
    import inspect

    from factory.engines.development.scaffold import generator

    for build in (generator.generate_scaffold, generator.generate_landing_page):
        assert "requirements" in inspect.signature(build).parameters, (
            f"{build.__name__} is judged against criteria it cannot see"
        )


async def test_one_place_answers_what_this_product_is_held_to() -> None:
    """Four callers each did their own lookup and decided whether to pass it on.

    One of them did not, which is how a builder and its reviewer came to hold
    different copies of the same standard. Resolving in one place is what makes that
    impossible -- the shape `design_for_product`/`effective_design` already uses.
    """
    import inspect

    from factory.engines.development import graph
    from factory.engines.development.requirements import requirements_for_product

    assert inspect.iscoroutinefunction(requirements_for_product)
    # No caller reaches around it to the raw row again.
    assert "live_requirements" not in inspect.getsource(graph), (
        "requirements are resolved through requirements_for_product, not re-derived"
    )
