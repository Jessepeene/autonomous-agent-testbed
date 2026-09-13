"""Issue identity — the thing whose absence filed one bug four times.

The failure this encodes, from production: the same broken checkout on one product
was reported on 09-12 at 06:43, again at 06:43, again at 13:38, again later, and
again on 09-13. Four work items, twice within the same minute, for one defect that
was still broken 24 days after it was first found. Nothing was failing to fix it.
Nothing could tell it was the same bug.

So these tests are about one question asked two ways: *does the same defect,
described differently, come back as the same issue* — and, just as important,
*do two genuinely different defects stay apart*. Getting the first wrong recreates
the bug. Getting the second wrong merges two real defects and the second never
gets fixed at all.
"""

from __future__ import annotations

import pytest

from factory import issues


class TestLocationNormalisation:
    """One endpoint, written the several ways a reviewer actually writes it."""

    @pytest.mark.parametrize(
        "written",
        [
            "POST /api/billing/checkout",
            "/api/billing/checkout",
            "api/billing/checkout",
            "  /API/Billing/Checkout/  ",
            "POST /api/billing/checkout?plan=pro",
        ],
    )
    def test_one_endpoint_written_five_ways_normalises_to_one(self, written: str) -> None:
        assert issues.normalise_location(written) == "api/billing/checkout"

    def test_an_id_in_a_path_is_an_example_not_a_distinguishing_feature(self) -> None:
        """`/orders/48291` and `/orders/50133` are the same defect location. Without
        this, every review of a page with an id in the URL files a fresh issue."""
        assert issues.normalise_location("/orders/48291") == issues.normalise_location(
            "/orders/50133"
        )

    def test_genuinely_different_paths_stay_different(self) -> None:
        assert issues.normalise_location("/api/billing/checkout") != issues.normalise_location(
            "/api/auth/register"
        )


class TestFingerprint:
    def test_the_same_defect_reworded_keeps_its_identity(self) -> None:
        """The core property. Two reviews, two descriptions, one endpoint."""
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

    def test_the_same_defect_on_a_different_product_is_a_different_issue(self) -> None:
        a = issues.fingerprint_for(
            product_id="p1", kind="bug", location="/api/checkout", title="broken"
        )
        b = issues.fingerprint_for(
            product_id="p2", kind="bug", location="/api/checkout", title="broken"
        )
        assert a != b

    def test_two_different_defects_on_one_product_stay_apart(self) -> None:
        """The failure mode in the other direction: merging two real defects means
        the second one is never fixed and never even visible."""
        checkout = issues.fingerprint_for(
            product_id="p1", kind="bug", location="/api/billing/checkout", title="422 on subscribe"
        )
        register = issues.fingerprint_for(
            product_id="p1", kind="bug", location="/auth/register", title="no token in response"
        )
        assert checkout != register

    def test_without_a_location_the_title_carries_identity(self) -> None:
        """Weaker, and the reason `looks_like_same_issue` exists as a second pass —
        but a reviewer who names no location must still get a stable issue rather
        than a new one every run."""
        a = issues.fingerprint_for(
            product_id="p1", kind="content", location="", title="Footer says 2025"
        )
        b = issues.fingerprint_for(
            product_id="p1", kind="content", location="", title="  footer says 2025  "
        )
        assert a == b


class TestWordingFallback:
    def test_the_same_defect_named_differently_is_still_matched(self) -> None:
        """When the fingerprint misses because the reviewer described the location
        instead of naming it, wording similarity is the second line of defence."""
        assert issues.looks_like_same_issue(
            "Subscribe button fails with a 422 error",
            "the subscribe button",
            "Subscribe button fails with 422 error on billing",
            "#subscribe-btn",
        )

    def test_two_unrelated_findings_are_not_merged(self) -> None:
        assert not issues.looks_like_same_issue(
            "Footer shows the wrong year",
            "footer",
            "Signup returns no auth token",
            "/auth/register",
        )

    def test_an_empty_finding_matches_nothing(self) -> None:
        """Fails closed: an empty description must not silently match the first open
        issue it is compared against."""
        assert not issues.looks_like_same_issue("", "", "Subscribe button fails", "#subscribe-btn")


class TestActionability:
    @pytest.mark.parametrize("kind", ["bug", "ui", "content", "regression"])
    def test_defects_may_commission_a_repair(self, kind: str) -> None:
        assert issues.is_actionable(kind)

    @pytest.mark.parametrize("kind", ["improvement", "feature_request"])
    def test_wishes_may_not(self, kind: str) -> None:
        """A feature request has no "expected behaviour" to restore, so a repair
        agent handed one would invent a specification and then build against it.
        Recorded so a manager sees the whole backlog; never auto-assigned."""
        assert not issues.is_actionable(kind)

    def test_every_actionable_kind_is_a_real_kind(self) -> None:
        assert set(issues.ACTIONABLE_KINDS) <= set(issues.KINDS)
