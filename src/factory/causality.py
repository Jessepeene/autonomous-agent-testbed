"""What caused what, without anyone having to remember to say so.

`task_events.caused_by_task_id` has existed since the trail was built, and its own
docstring calls it *"the column that makes this a trail rather than another log: it
answers 'what caused what', which no existing query can."* On 2026-09-22 it was
populated in **0 of 232 rows**.

Both ends were built. `supervisor/graph.py` reads `caused_by_task_id` out of a task's
payload and records it against every event that task emits. `repository.enqueue_task`
accepts a payload. Nothing in between ever put the parent's id into a child's payload,
at any of the call sites, ever -- so the column recorded the absence of a convention
rather than the absence of a cause.

**Which is why this is a context variable and not an argument.** The per-call-site
version is the pattern this codebase keeps paying for: a rule that has to be
remembered at each new call site is a rule that holds until somebody is busy. It cost
five unguarded recall sites, a policy line graded as an acceptance criterion in three
separate judges, and this column. Set once where a task begins, every enqueue inside
it inherits the link, including the ones nobody has written yet.

`contextvars` is the right mechanism rather than a global: `asyncio` copies the
context into each task it spawns, so two engines running concurrently each see their
own id, and a child setting one cannot reach back into its parent. A caller outside
any task -- the scheduler, the dashboard API -- reads `None` and its work is correctly
recorded as having no parent, because it has none.

The chain this produces is what attribution needs (ARCHITECTURE_ROADMAP.md, Phase
2.14): an outcome can only be evidence about the decisions that led to it if
something knows which decisions those were.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: The task whose handler is currently running, if any.
_current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)

#: Payload key carrying the parent link. Read by `supervisor/graph.py` when the child
#: is claimed, so the value travels with the work rather than being re-derived.
CAUSE_KEY = "caused_by_task_id"


def current_task_id() -> str | None:
    """The task being handled right now, or `None` outside any task handler."""
    return _current_task_id.get()


@contextmanager
def running(task_id: str) -> Iterator[None]:
    """Mark `task_id` as the work currently in progress, for its whole handler.

    Restores the previous value on exit rather than clearing it, so nesting behaves
    -- a handler that runs another handler inline does not erase its own identity on
    the way out.
    """
    token = _current_task_id.set(task_id)
    try:
        yield
    finally:
        _current_task_id.reset(token)


def attribute(payload: dict[str, object] | None) -> dict[str, object]:
    """A task's payload with its parent link filled in, if there is one.

    Never overwrites an explicit value: a caller that knows the real cause better than
    the ambient one -- a retry standing in for the task it replaces, say -- keeps what
    it passed.
    """
    out = dict(payload or {})
    cause = current_task_id()
    if cause and not out.get(CAUSE_KEY):
        out[CAUSE_KEY] = cause
    return out
