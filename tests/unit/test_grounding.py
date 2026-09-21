"""A claim about the standard has to be a claim about something in the standard.

The two cases pinned first are the two that actually happened, on prod, on
2026-09-21. A gap reviewer was handed criteria, deliberate non-goals and standing
policy as one block under a heading calling all of it "the acceptance criteria this
product was built to meet", and reported two MUSTs unmet:

- a `PAGE_POLICY` line, word for word -- a sentence explaining why a provisional
  canonical URL is correct, filed as a defect;
- an out-of-scope entry, reported as missing, because `RequirementGap.tier` is
  `Literal["must", "should"]` and a reviewer correctly told that out-of-scope
  violations are departures had nowhere else to put one.

Both were unsatisfiable by construction. Two repair cycles moved nothing, the
release decision refused to spend a third, and a human had to break the loop.

Neither was the model being careless. It was told those lines were criteria and it
graded them, and `GAP_SYSTEM`'s "Never invent a criterion" was an instruction in a
prompt standing in for a check in code.
"""

from __future__ import annotations

from factory.grounding import DEFAULT_THRESHOLD, ground, overlap

#: The real policy bullet, and the real truncation of it that reached the issues
#: table as `MUST not met: ...`.
POLICY_LINE = (
    "The page does not know its own final address. It is deployed to a Vercel "
    "subdomain first and a custom domain is attached afterwards, by a person, so a "
    "canonical URL or og:url that is not the eventual public domain is expected and "
    "correct at build time. It is never a defect in the page and never something to "
    "repair."
)

CATALOGUE = {
    "must": [
        "A visitor can enter their email address and submit it to express interest.",
        "After submission, the visitor sees a confirmation message telling them to "
        "expect an email.",
    ],
    "should": ["The page includes a call-to-action button using plain language."],
    "out_of_scope": ["A pricing page or price display"],
    "policy": [POLICY_LINE],
}


def test_a_policy_line_grounds_to_policy_not_to_a_criterion() -> None:
    """Live case one. Reported as `MUST not met`; it is a policy line, and no product
    can leave one unmet."""
    placed = ground(POLICY_LINE, CATALOGUE)
    assert placed.group == "policy"


def test_a_truncated_quote_still_grounds_to_its_source() -> None:
    """The form it actually arrived in -- the issue title cut the sentence off at 150
    characters. Jaccard scores this pair 0.5 and calls them different statements,
    purely because one is shorter; overlap reads it as the quote it is."""
    truncated = POLICY_LINE[:150]
    assert overlap(truncated, POLICY_LINE) >= DEFAULT_THRESHOLD
    assert ground(truncated, CATALOGUE).group == "policy"


def test_an_out_of_scope_entry_grounds_to_out_of_scope() -> None:
    """Live case two. Reported as `MUST not met: OUT OF SCOPE: A pricing page or
    price display` -- a thing deliberately not built, filed as missing."""
    placed = ground("OUT OF SCOPE: A pricing page or price display", CATALOGUE)
    assert placed.group == "out_of_scope"


def test_a_real_criterion_still_grounds_to_must() -> None:
    """The guard against the fix being a mute button: a genuine unmet MUST must keep
    blocking, including when the reviewer paraphrases it rather than quoting."""
    placed = ground(
        "A visitor can enter an email address and submit it to register interest",
        CATALOGUE,
    )
    assert placed.group == "must"
    assert placed.entry == CATALOGUE["must"][0]


def test_something_in_no_list_grounds_nowhere() -> None:
    """Not a failure -- this is a reviewer noticing something the standard never
    mentioned, which is exactly what a criteria-shaped gate is blind to. It must be
    distinguishable from a criterion, not discarded."""
    placed = ground("The signup form posts over plain HTTP on the staging subdomain", CATALOGUE)
    assert placed.group is None
    assert not placed.found


def test_a_claim_too_short_to_place_is_not_placed() -> None:
    """Two or three common words coincide with almost anything. Matching on that
    would put claims under whichever criterion happened to share a noun, which is the
    same failure in the other direction."""
    assert ground("the page", CATALOGUE).group is None


def test_grounding_never_raises_on_an_empty_catalogue() -> None:
    """Callers without a standard to check against exist, and must get a clean
    "placed nowhere" rather than an exception on a review path."""
    assert ground("anything at all, said at some length", {}).group is None


# ------------------------------------------- the catalogue must cover the standard


def test_a_design_criterion_grounds_as_a_blocking_must() -> None:
    """The way this fix could have become a mute button.

    `must_hold` is graded by the very same pass that grades `must` -- "the page loads
    at least one webfont" is a real blocking criterion. Left out of the catalogue it
    would ground nowhere, and a genuine design gap would have been quietly downgraded
    to an observation by a change meant to stop over-blocking. That defect is the one
    a human had to catch because no automated judge could see it.
    """
    from factory.engines.development.requirements import HOUSE_STYLE, standard_catalogue
    from factory.state.schemas import AcceptanceCriterion, WorkRequirements

    requirements = WorkRequirements(
        goal="Test whether anybody wants a demand forecast for tomorrow's baking.",
        must=[
            AcceptanceCriterion(
                criterion="A visitor can enter their email address and submit it.",
                why="It is the only action the page can ask for.",
            )
        ],
    )
    catalogue = standard_catalogue(requirements, design=HOUSE_STYLE)

    placed = ground(
        "The page loads at least one webfont; a system-font-only stack is not a typographic choice",
        catalogue,
    )
    assert placed.group == "must", "a design criterion must keep blocking"


def test_a_must_avoid_item_blocks_and_is_not_mistaken_for_scope_creep() -> None:
    """`must_avoid` and `out_of_scope` are both "present is a departure", and they
    are not the same thing. A page built from a framework's stock ramp did not do
    extra work nobody asked for -- it is the stated defect the design guidelines
    exist to make checkable, so it blocks."""
    from factory.engines.development.gaps import _DEPARTURE
    from factory.engines.development.requirements import standard_catalogue
    from factory.state.schemas import (
        AcceptanceCriterion,
        DesignGuidelines,
        WorkRequirements,
    )

    design = DesignGuidelines(
        direction="Borrow from the bakery's own world: proof times, trays, order books.",
        palette=["#2B1A12", "#E8D9C5"],
        typography="A warm serif for display, a plain grotesk for body.",
        must_hold=["The palette is specific to this product"],
        must_avoid=["A purple-to-blue gradient hero on white"],
    )
    requirements = WorkRequirements(
        goal="Test whether anybody wants a demand forecast for tomorrow's baking.",
        must=[
            AcceptanceCriterion(
                criterion="A visitor can enter their email address and submit it.",
                why="It is the only action the page can ask for.",
            )
        ],
        out_of_scope=["A pricing page or price display"],
    )
    catalogue = standard_catalogue(requirements, design=design)

    assert ground("A purple-to-blue gradient hero on white", catalogue).group == "must_avoid"
    assert _DEPARTURE["must_avoid"][0] == "high"
    assert _DEPARTURE["out_of_scope"][0] == "low"


def test_every_group_the_catalogue_emits_has_a_departure_rule() -> None:
    """The rule, not the values -- the same test shape that would have caught DESIGN
    missing from `routing.PRIORITY`. Adding a group to the catalogue without saying
    how a departure from it is recorded would raise a KeyError on a live review path.
    """
    from factory.engines.development.gaps import _DEPARTURE
    from factory.engines.development.requirements import HOUSE_STYLE, standard_catalogue
    from factory.state.schemas import AcceptanceCriterion, WorkRequirements

    catalogue = standard_catalogue(
        WorkRequirements(
            goal="Test whether anybody wants a demand forecast for tomorrow's baking.",
            must=[AcceptanceCriterion(criterion="A visitor can sign up.", why="The one action.")],
        ),
        design=HOUSE_STYLE,
    )
    missing = set(catalogue) - set(_DEPARTURE)
    assert not missing, f"no departure rule for {missing}"
