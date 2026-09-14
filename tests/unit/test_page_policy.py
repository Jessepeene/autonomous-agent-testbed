"""Standing policy reaches every judge, and overrides stale criteria.

The permanent fix for a class of failure, so the property is worth pinning rather
than trusting.

Policy and criteria are different kinds of fact. "Never publish where the idea
came from" is true of every page this factory will ever build and changes when the
business changes its mind; "state the $199 price" is true of one product and
correct to freeze at generation time. They were mixed, and it cost a day:

- the policy was re-decided per product by a generator that had not been told it,
  so it varied;
- it was frozen into stored criteria, so fixing the prompt did nothing for pages
  already specified;
- and the generator got it *wrong*, writing "SHOULD: the page links to the real
  public issue thread used as the need signal" -- which put an internal,
  scraped-from URL in front of customers, because a build is judged against its
  criteria and the builder obeyed them over its own instructions.

Rendering policy live, on every read, means a change takes effect on the next build
everywhere with nothing to regenerate -- and a stale criterion cannot outrank it.
"""

from __future__ import annotations

from factory.engines.development.requirements import PAGE_POLICY, render_for_prompt
from factory.state.schemas import AcceptanceCriterion, WorkRequirements


def _requirements(**overrides: object) -> WorkRequirements:
    base = {
        "goal": "Find out whether mobile engineering leads want an iOS test sharding tool",
        "must": [
            AcceptanceCriterion(
                criterion="A visitor can submit their email address",
                why="The page exists to collect that signal",
            )
        ],
    }
    base.update(overrides)
    return WorkRequirements(**base)  # type: ignore[arg-type]


def test_policy_travels_with_every_rendering() -> None:
    """`render_for_prompt` is the single chokepoint the builder, gap check, live
    review and pre-ship review all read through. Policy attached here reaches all
    four, which is what makes a policy change take effect without regenerating
    anything."""
    rendered = render_for_prompt(_requirements())

    assert PAGE_POLICY in rendered
    # And the product-specific half is still there -- policy supplements criteria,
    # it does not replace them.
    assert "A visitor can submit their email address" in rendered


def test_policy_states_that_it_overrides_a_criterion() -> None:
    """A stale criterion must not be able to outrank current policy.

    The live criteria for a real product still demand the provenance link. Policy
    saying the opposite is only useful if the reader is told which wins -- otherwise
    it is two instructions in one prompt and the specific one usually wins.
    """
    assert "override" in PAGE_POLICY.lower()
    assert "the policy wins" in PAGE_POLICY.lower()


def test_the_four_standing_rules_are_all_present() -> None:
    """Each of these was a real defect on a real deployed page, not a hypothetical."""
    lowered = PAGE_POLICY.lower()

    # Jesse found this one himself, twice: the page linked the GitHub thread the
    # idea was scraped from.
    assert "issue tracker" in lowered or "where the idea came from" in lowered
    # Never shipped, and must not be: the conventional-smoke-test framing Jesse
    # chose makes fabricated social proof the obvious next step, and it is the line.
    assert "testimonial" in lowered
    # The page collects an email and nothing else.
    assert "buy today" in lowered
    # Jesse's original finding: "ShardRunner is not available yet."
    assert "present tense" in lowered


def test_a_criterion_contradicting_policy_is_still_visible_to_the_reader() -> None:
    """The failure mode this must survive, end to end.

    Superseding the stored criteria fixes today's page. This test is about tomorrow's:
    a generator that writes the same bad criterion again produces a prompt where the
    policy sits directly beside it, marked as winning.
    """
    contradicting = _requirements(
        should=[
            AcceptanceCriterion(
                criterion="The page links to the real public issue thread used as the need signal",
                why="Shows the problem is real",
            )
        ]
    )

    rendered = render_for_prompt(contradicting)

    assert "links to the real public issue thread" in rendered
    assert PAGE_POLICY in rendered
    # The policy block comes last, so it is the final instruction the reader sees.
    assert rendered.index(PAGE_POLICY) > rendered.index("links to the real public issue thread")


def test_no_page_building_prompt_receives_the_scraped_source_url() -> None:
    """The simplest guarantee available: you cannot leak what you never had.

    It was passed to all three prompts -- the requirements writer and both codegen
    paths -- each time labelled as internal and with an explicit instruction not to
    cite it. A page cited it anyway ("read the corroborating discussion in this App
    Center issue"), and criteria written from the same field demanded it.

    Two rounds of better warnings did not fix it; not handing it over does. Nothing
    in a page-building prompt needs to know where the idea came from: criteria and
    copy describe the product, and provenance cannot inform either.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "factory"
    prompt_files = [
        root / "engines" / "development" / "requirements.py",
        root / "engines" / "development" / "scaffold" / "generator.py",
    ]

    for path in prompt_files:
        body = path.read_text(encoding="utf-8")
        # Comments explaining the removal are fine; an interpolation is not.
        interpolations = [
            line
            for line in body.splitlines()
            if "{idea.source_url}" in line and not line.strip().startswith("#")
        ]
        assert not interpolations, (
            f"{path.name} interpolates idea.source_url into a prompt: {interpolations}. "
            "A page-building prompt must not receive it -- labelling it as internal "
            "was tried twice and a generated page published it both times."
        )
