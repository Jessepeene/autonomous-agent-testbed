"""What caused what, filled in by being inside the work rather than by being told.

`task_events.caused_by_task_id` was populated in **0 of 232 rows** on 2026-09-22.
Both ends had been built -- `supervisor/graph.py` reads the parent out of a claimed
task's payload, `enqueue_task` accepts a payload -- and no call site in the codebase
ever put one id into the other. The column recorded the absence of a convention, not
the absence of a cause.

So the link is ambient rather than a parameter. The parameter version is the pattern
this codebase keeps paying for: it cost five unguarded recall sites, a policy line
graded as a criterion in three separate judges, and this column.
"""

from __future__ import annotations

import asyncio

from factory import causality


def test_outside_any_task_there_is_no_parent() -> None:
    """The scheduler and the dashboard API enqueue work that genuinely has no
    parent. That must read as no parent, not as a missing link."""
    assert causality.current_task_id() is None
    assert causality.attribute({"product_id": "p-1"}) == {"product_id": "p-1"}


def test_work_enqueued_inside_a_task_carries_its_parent() -> None:
    with causality.running("task-parent"):
        payload = causality.attribute({"product_id": "p-1"})
    assert payload == {"product_id": "p-1", causality.CAUSE_KEY: "task-parent"}


def test_an_explicit_cause_is_never_overwritten() -> None:
    """A caller that knows the real cause better than the ambient one -- a retry
    standing in for the task it replaces -- keeps what it passed."""
    with causality.running("task-parent"):
        payload = causality.attribute({causality.CAUSE_KEY: "the-real-cause"})
    assert payload[causality.CAUSE_KEY] == "the-real-cause"


def test_an_empty_payload_still_gets_its_parent() -> None:
    with causality.running("task-parent"):
        assert causality.attribute(None) == {causality.CAUSE_KEY: "task-parent"}


def test_the_payload_passed_in_is_not_mutated() -> None:
    """Callers pass literals and reuse dicts; filling one in place would attribute
    later work to whatever ran first."""
    original: dict[str, object] = {"product_id": "p-1"}
    with causality.running("task-parent"):
        causality.attribute(original)
    assert original == {"product_id": "p-1"}


def test_identity_is_restored_after_the_task_finishes() -> None:
    """Restored rather than cleared, so a handler that runs another handler inline
    does not erase its own identity on the way out."""
    with causality.running("outer"):
        with causality.running("inner"):
            assert causality.current_task_id() == "inner"
        assert causality.current_task_id() == "outer"
    assert causality.current_task_id() is None


def test_two_engines_running_at_once_do_not_see_each_others_work() -> None:
    """The reason this is a context variable and not a global. `asyncio` copies the
    context into each task it spawns, so concurrent engines each attribute their own
    work -- and a global would cross the wires silently, which is worse than the
    empty column this replaces."""

    async def engine(name: str, seen: dict[str, str | None]) -> None:
        with causality.running(name):
            await asyncio.sleep(0)  # force interleaving
            seen[name] = causality.current_task_id()

    async def both() -> dict[str, str | None]:
        seen: dict[str, str | None] = {}
        await asyncio.gather(engine("task-a", seen), engine("task-b", seen))
        return seen

    assert asyncio.run(both()) == {"task-a": "task-a", "task-b": "task-b"}
