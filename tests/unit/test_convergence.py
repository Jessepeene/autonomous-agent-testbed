"""Whether work is getting anywhere, judged from rows rather than remembered.

`MAX_HEAL_CYCLES = 3` decided when to stop, and a counter cannot tell the two
situations apart that actually matter: a run that closed two blockers and opened
none at attempt five is progress, and one that has re-found the same unmet criterion
three times running is a wall. Only one of those is worth another attempt.

This is also the shape the continuity problem has to take. A manager holding days of
context in a shrinking window is blinder than the judges it replaces the moment
compression drops something, and is trusted more -- compression fails invisibly,
where a query fails visibly. Every field here is a count over real rows, so nothing
in it can be hallucinated.
"""

from __future__ import annotations

from factory.state.repository import ProductProgress


def _progress(**overrides: object) -> ProductProgress:
    base: dict[str, object] = {
        "attempts": 2,
        "blocking_now": 3,
        "blocking_at_last_attempt": 5,
        "stuck": [],
        "last_decision": "fix_first",
    }
    base.update(overrides)
    return ProductProgress(**base)  # type: ignore[arg-type]


def test_a_shrinking_blocking_set_is_converging() -> None:
    assert _progress(blocking_now=3, blocking_at_last_attempt=5).converging is True


def test_a_blocking_set_that_held_is_not_converging() -> None:
    """Unchanged is a wall, not progress. This is the case a counter reads as "you
    still have one attempt left" and a person reads as "this is not working"."""
    assert _progress(blocking_now=4, blocking_at_last_attempt=4).converging is False


def test_a_growing_blocking_set_is_not_converging() -> None:
    """Observed live: 19 issues became 28 in a single cycle, each regeneration
    trading one blocker set for another while the counter cheerfully ticked."""
    assert _progress(blocking_now=9, blocking_at_last_attempt=4).converging is False


def test_the_first_attempt_has_nothing_to_compare_against() -> None:
    """`None`, not `False`. Convergence is a direction, and one point has none --
    calling a first attempt "not converging" would stop work that never started."""
    assert _progress(blocking_at_last_attempt=None).converging is None


def test_stuck_issues_are_the_sharper_signal() -> None:
    """Not "we have tried a lot" but "we keep failing at the same thing". A heal
    counter cannot express the difference; `times_seen` can."""
    progress = _progress(stuck=["MUST not met: the page states the price"])
    assert progress.stuck


def test_progress_carries_the_previous_decision() -> None:
    """So a second look starts from the first rather than re-deriving it -- the
    thread across attempts, without anything having to remember it."""
    assert _progress(last_decision="rethink").last_decision == "rethink"


class TestWhenHealingStops:
    """The counter that ended a run which had already succeeded.

    `_heal` escalated on `attempts >= max_heal_cycles`, independently of anything
    that knew whether the work was going anywhere. Observed live on a blank-slate
    landing page: by attempt three the workspace passed the contract check and the
    SEO check with zero violations and carried no backend, no auth and no Stripe --
    the gap had been fixed -- and it escalated anyway.

    The release judgement had convergence and could say rethink. The counter fired
    first and never consulted it. Same shape as every other bug found that day: a
    blind rule overruling an informed judgement.
    """

    def test_work_that_is_closing_blockers_is_not_a_wall(self) -> None:
        progress = _progress(blocking_now=1, blocking_at_last_attempt=4, stuck=[])
        walled = progress.converging is False or bool(progress.stuck)
        assert not walled, "a run that is closing blockers should keep going"

    def test_a_blocking_set_that_has_not_moved_is_a_wall(self) -> None:
        progress = _progress(blocking_now=4, blocking_at_last_attempt=4, stuck=[])
        assert progress.converging is False or bool(progress.stuck)

    def test_the_same_criterion_failing_repeatedly_is_a_wall(self) -> None:
        """Even while the overall count falls. Trading one blocker for another looks
        like progress in aggregate and is not."""
        progress = _progress(
            blocking_now=3,
            blocking_at_last_attempt=4,
            stuck=["MUST not met: a visitor can submit an email"],
        )
        assert progress.converging is True
        assert bool(progress.stuck), "a criterion that keeps failing is its own signal"

    def test_a_first_attempt_is_never_a_wall(self) -> None:
        """`converging` is None with nothing to compare against, and `None is False`
        is False -- so a first attempt continues rather than being stopped by a
        comparison that does not exist yet."""
        progress = _progress(blocking_at_last_attempt=None, stuck=[])
        assert not (progress.converging is False or bool(progress.stuck))
