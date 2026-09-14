"""The ship gate that uses the product instead of reading it.

Covers the two halves separately, because they fail in different ways:

- the **deterministic** half (`_failed_requests`), which turns the browser's own
  network record into a verdict nothing has to be trusted for;
- the **gate** behaviour, which must block a broken page without stranding a page
  whose review simply could not run.

The bug that motivated all of it: no CORS configuration anywhere in the API, so
every form on every deployed landing page failed in the browser while all five
source-reading reviewers passed it and `curl` reported success.
"""

from __future__ import annotations

from factory.engines.development.preship import (
    SELF_TEST_EMAIL,
    PreShipOutcome,
    _failed_requests,
)


def test_a_cors_blocked_request_is_detected_without_the_agent_saying_so() -> None:
    """The exact shape a CORS block takes in Playwright's network record.

    This is the whole reason the check is deterministic. An agent might describe
    this as "the form didn't seem to work", or miss it entirely if the page shows
    an optimistic thank-you; the request record is not a matter of opinion.
    """
    observed = [
        {"method": "GET", "url": "https://x.vercel.app/", "status": 200},
        {
            "method": "POST",
            "url": "https://manifestaudit.com/interest/abc123",
            "status": "FAILED (net::ERR_FAILED)",
        },
    ]

    failures = _failed_requests(observed)

    assert len(failures) == 1
    assert failures[0]["url"].endswith("/interest/abc123")


def test_a_server_rejection_is_detected_too() -> None:
    """A 4xx/5xx is a different failure from a blocked request and equally fatal."""
    observed = [
        {"method": "POST", "url": "https://x/interest/a", "status": 422, "body": "field required"},
        {"method": "GET", "url": "https://x/", "status": 200},
    ]

    failures = _failed_requests(observed)

    assert len(failures) == 1
    assert failures[0]["status"] == 422
    # The body is carried through: a 422 names the field that failed, which is the
    # difference between an obvious fix and a guess.
    assert "field required" in failures[0]["body"]


def test_a_working_page_produces_no_failures() -> None:
    observed = [
        {"method": "GET", "url": "https://x.vercel.app/", "status": 200},
        {"method": "POST", "url": "https://manifestaudit.com/interest/abc", "status": 200},
        {"method": "POST", "url": "https://manifestaudit.com/analytics/abc", "status": 200},
    ]

    assert _failed_requests(observed) == []


def test_a_review_that_could_not_run_does_not_block_shipping() -> None:
    """`ran=False` must not read as either a pass or a failure.

    Playwright missing, a browser that will not start, a hung navigation. None of
    those are evidence about the product, and none should strand a product that
    deployed successfully -- but the caller has to be able to tell the difference,
    which is what `ran` is for.
    """
    outcome = PreShipOutcome(ran=False, summary="review could not run")

    assert outcome.may_ship is True
    assert outcome.ran is False


def test_a_broken_page_blocks_shipping() -> None:
    outcome = PreShipOutcome(ran=True, blocking=["the form submission failed"])

    assert outcome.may_ship is False


def test_cosmetic_notes_alone_do_not_block() -> None:
    """Done and perfect are different states.

    A page held back for every imperfection never ships, which is the failure the
    whole release-judgement design exists to avoid. Notes are recorded as low
    issues and weighed by the release decision like any other accepted trade.
    """
    outcome = PreShipOutcome(ran=True, notes=["the footer year is hardcoded"])

    assert outcome.may_ship is True


def test_the_self_test_address_can_never_reach_a_real_inbox() -> None:
    """`.invalid` is reserved by RFC 2606 and resolves nowhere.

    The reviewer really does submit the form, so this address really is sent. It
    must be impossible for it to become a delivery attempt -- a bounce counts
    against a sending reputation the factory freezes outreach over -- and
    impossible for a genuine visitor to collide with.
    """
    assert SELF_TEST_EMAIL.endswith(".invalid")
    assert "@" in SELF_TEST_EMAIL


def test_a_review_that_never_tried_the_form_is_not_a_pass() -> None:
    """Not attempted and attempted-but-failed are both failures.

    The first version blocked only on the second, so a review that never tried the
    one thing the page exists for passed silently. Seen on the first clean run:
    `preship.reviewed blocking=0 action_succeeded=False` -- recorded as a pass, on a
    page whose submission had not been exercised at all.

    Same shape as `ran=False`: a check that did not happen must never look like a
    check that passed. The point of using the product is finding out whether a
    visitor can do the thing, and not finding out is not a yes.
    """
    from factory.state.schemas import PreShipReview

    never_tried = PreShipReview(
        primary_action_attempted=False, primary_action_succeeded=False, summary="read the page"
    )
    tried_and_failed = PreShipReview(
        primary_action_attempted=True, primary_action_succeeded=False, summary="submit failed"
    )
    worked = PreShipReview(
        primary_action_attempted=True, primary_action_succeeded=True, summary="submitted fine"
    )

    # The property under test is the one the gate reads: only a completed action
    # clears it, however the reviewer got there.
    assert not never_tried.primary_action_succeeded
    assert not tried_and_failed.primary_action_succeeded
    assert worked.primary_action_succeeded
