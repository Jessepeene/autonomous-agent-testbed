"""Which Bug Council findings may stop a deploy.

This changes a hard rail, so the tests are written around what must stay true
rather than around the new behaviour.

The failure that forced the change: a correct fix for a broken
`POST /api/billing/checkout` was written on 12 September and never shipped. It sat
behind 8 unrelated performance, quality and dependency findings in the same file
while the live product returned 422 to every paying customer for another full day,
and three heal attempts exhausted themselves trying to clear that backlog before
they could ship the one fix that mattered.

The reviewer prompt sets `approved=false` at "medium severity or above", and every
finding from an unapproved reviewer used to block. So one medium nit could hold a
product indefinitely, and the more the reviewers found, the less likely anything
was ever to ship.

A human team ships the hotfix and files the rest. That is only safe if the rest
genuinely are filed -- which is what `_record_council_findings` guarantees, and why
the recording test below matters as much as the gating ones.
"""

from __future__ import annotations

import pytest

from factory.state.schemas import (
    BLOCKING_SEVERITIES,
    STRICT_CATEGORIES,
    CouncilResult,
    ReviewCategory,
    ReviewFinding,
    ReviewVerdict,
)


def _finding(category: str, severity: str) -> ReviewFinding:
    return ReviewFinding(
        category=ReviewCategory(category),
        severity=severity,  # type: ignore[arg-type]
        file="app/main.py",
        line=42,
        summary=f"A {severity} {category} finding that needs describing properly.",
        failure_scenario="Under concurrent load the handler returns a 500 to the caller.",
        remediation="Bound the queue and return 429 when it is full instead.",
    )


def _council(*findings: ReviewFinding) -> CouncilResult:
    return CouncilResult(
        approved=not findings,
        verdicts=[
            ReviewVerdict(
                category=f.category,
                approved=False,
                findings=[f],
            )
            for f in findings
        ],
    )


@pytest.mark.parametrize("severity", sorted(BLOCKING_SEVERITIES))
@pytest.mark.parametrize(
    "category", ["security", "performance", "dependencies", "privilege", "secrets", "quality"]
)
def test_serious_findings_still_block_in_every_category(severity: str, category: str) -> None:
    """The rail that does not move. Nothing critical or high ships, whatever it is
    about."""
    council = _council(_finding(category, severity))
    assert council.deploy_blocking


@pytest.mark.parametrize("category", sorted(STRICT_CATEGORIES))
def test_a_medium_finding_that_can_leak_still_blocks(category: str) -> None:
    """Security, secrets and privilege are stricter on purpose. This system handles
    real payments and real personal data, every other guard in it fails closed, and
    shipping a known hole is a human's call to make, not the factory's."""
    council = _council(_finding(category, "medium"))
    assert council.deploy_blocking


@pytest.mark.parametrize("category", ["performance", "dependencies", "quality"])
def test_a_medium_finding_elsewhere_no_longer_holds_the_product(category: str) -> None:
    """The three categories that trapped a working checkout fix for a day. Real
    findings, worth recording -- never worth holding a broken payment path hostage
    to."""
    council = _council(_finding(category, "medium"))
    assert council.deploy_blocking == []
    # Still surfaced, so `_record_council_findings` files it.
    assert len(council.blocking) == 1


@pytest.mark.parametrize("category", ["performance", "dependencies", "quality"])
def test_low_findings_never_block(category: str) -> None:
    assert _council(_finding(category, "low")).deploy_blocking == []


def test_one_serious_finding_among_many_minor_ones_still_blocks() -> None:
    """The realistic shape. Severity gating must not become "mostly minor, ship it"
    -- a single high finding anywhere in the set stops the deploy."""
    council = _council(
        _finding("performance", "medium"),
        _finding("quality", "low"),
        _finding("dependencies", "medium"),
        _finding("performance", "high"),
    )
    blocking = council.deploy_blocking
    assert len(blocking) == 1
    assert blocking[0].severity == "high"


def test_nothing_found_means_nothing_blocks() -> None:
    assert _council().deploy_blocking == []


def test_findings_from_an_approving_reviewer_are_not_blocking() -> None:
    """A reviewer that approved has, by definition, not withheld the build. Its
    notes are still findings, but they were never the gate."""
    council = CouncilResult(
        approved=True,
        verdicts=[
            ReviewVerdict(
                category=ReviewCategory.PERFORMANCE,
                approved=True,
                findings=[_finding("performance", "critical")],
            )
        ],
    )
    assert council.blocking == []
    assert council.deploy_blocking == []
