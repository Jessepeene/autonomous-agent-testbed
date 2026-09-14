"""The contract callers of `record_issue` have to keep: give a *precise* location.

Issue identity is load-bearing well past tidiness. `times_seen` feeds `stuck`,
`stuck` makes `_heal` declare a wall, and a wall escalates the product -- so
getting identity wrong does not produce a messy list, it stops builds.

`fingerprint_for` keys on the normalised location whenever one is given, and that
is the right design: it is what lets one defect described two different ways stay
one issue, which is the property that stopped a broken checkout being filed four
times as four unrelated work items. The cost of that design is a contract on the
caller, and the contract is not enforceable in code -- a location is just a
string, and no check can tell `index.html:168` from `index.html` by type.

So it is pinned here instead, with the mistake that motivated it. Filing two
unrelated defects against `deployed: index.html` merged them into one row with
`times_seen = 2`, which reads as "seen twice" and means "two different things".
Every real caller in the codebase already gets this right -- Bug Council passes
`file:line`, the contract and SEO checks embed the violating item, the gap check
embeds the criterion. A one-off script passed a bare filename, and that was enough.
"""

from __future__ import annotations

from factory import issues


def test_a_precise_location_keeps_two_defects_apart() -> None:
    """What every real caller does, and why it works.

    Bug Council files `index.html:168` and `index.html:0`; the contract check
    embeds the violating requirement in the location. Distinct defects get
    distinct locations, so they get distinct fingerprints.
    """
    first = issues.fingerprint_for(
        product_id="p1", kind="ui", location="index.html:168", title="Hardcoded copyright year"
    )
    second = issues.fingerprint_for(
        product_id="p1", kind="ui", location="index.html:12", title="Internal link left in copy"
    )

    assert first != second


def test_a_bare_filename_collapses_unrelated_defects() -> None:
    """The failure mode, pinned so it is a known cost rather than a surprise.

    This is not a bug in `fingerprint_for` -- location-keyed identity is what makes
    re-wordings match, and that property is worth more than this. It is the caller's
    obligation, and the reason it is worth a test is that the symptom appears far
    from the cause: an inflated `times_seen` shows up later as a phantom wall in
    `_heal`, with nothing pointing back at the location string that caused it.
    """
    coarse = "deployed: index.html"
    first = issues.fingerprint_for(
        product_id="p1",
        kind="ui",
        location=coarse,
        title="The page publishes the factory's own internal provenance to visitors",
    )
    second = issues.fingerprint_for(
        product_id="p1",
        kind="ui",
        location=coarse,
        title="The page tells visitors the product does not exist",
    )

    assert first == second, (
        "a whole-file location cannot distinguish defects -- callers must pass "
        "something specific enough to identify one defect, e.g. file:line, a "
        "selector, an endpoint, or the violating text itself"
    )
    # And the similarity pass would have known better, which is what makes this
    # purely a caller mistake: it judges these two distinct (jaccard 0.33 against a
    # 0.6 bar). The fingerprint matched first, so that judgement never ran.
    assert not issues.looks_like_same_issue(
        "The page publishes the factory's own internal provenance to visitors",
        coarse,
        "The page tells visitors the product does not exist",
        coarse,
    )


def test_one_defect_described_two_ways_stays_one_issue() -> None:
    """The property location-keyed identity exists to provide, and must keep.

    Two reviews, two descriptions, one endpoint. Losing this is what produced 24
    days of a broken checkout being rediscovered as a fresh finding every time.
    """
    first = issues.fingerprint_for(
        product_id="p1",
        kind="bug",
        location="POST /api/billing/checkout",
        title="Subscribe button returns 422 and shows [object Object]",
    )
    second = issues.fingerprint_for(
        product_id="p1",
        kind="bug",
        location="/api/billing/checkout",
        title="The $199/mo checkout flow is broken on the billing endpoint",
    )

    assert first == second
