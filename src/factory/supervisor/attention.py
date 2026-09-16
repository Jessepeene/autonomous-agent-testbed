"""Telling a department there is something it might own. Not telling it what to do.

Design has never had an inbox. Every task it has ever run was enqueued by its own
24-hour tick, so the only way anything reached it was for it to wake on a timer and go
looking. On 2026-09-15 that produced the sharpest version of the failure: its manager
ran at 00:00:07, could see a product with no guidelines, and correctly commissioned
nothing -- the page had been built, reviewed, repaired and closed 94 minutes earlier,
and guidelines written for a shipped page are documentation, not direction. Its
judgement was right and its position in time was wrong, which is the one failure mode
that cannot be fixed by making the department better at its job.

**This module is mechanism, and deliberately holds no judgement at all.** It is handed
a list of needs and wakes whoever owns them. The first version derived that list itself
from the product's unmet facts, which made it a resolver: every absence became work,
and the ordering between departments was implied by a dependency graph instead of
decided by anyone. `supervisor/product_decision.py` makes the decision; this carries it.

The caller side stays clean either way: `route_attention` names no department. `OWNER`
in `factory.facts` names them, so a decision to pursue a need reaches its owner without
the decider knowing who that is, and a sixth department would not touch this file.

Addressed to a department's **manager**, never its workers. You can ask a department to
look at something; you cannot hand it its work. That is the difference between an
organisation and a pipeline whose edges happen to be spelled as function calls.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from factory.facts import OWNER, Unmet, product_facts
from factory.logging import get_logger
from factory.state import repository

log = get_logger(__name__)


async def route_attention(
    session: AsyncSession, product_id: str, *, needs: Sequence[Unmet], asked_by: str
) -> list[Unmet]:
    """Wake the manager that owns each of these needs. Pure mechanism, no judgement.

    ``needs`` is passed in rather than derived here, and that changed after Jesse read
    the first version: *"the system should be smart enough to know design needs to
    finish before dev without us needing to hard code it."* Deriving the list here made
    this a resolver -- every unmet fact became work, which is the conveyor with the
    ordering hidden in a dependency graph. Something has to *decide* which needs are
    worth pursuing now, and that decision is `supervisor/product_decision.py`. This
    function only carries the result.

    Returns the needs actually routed. A need with no owner is silently not routed and
    is not an error: `Unmet.AUDIENCE` has no department today, and inventing one to
    receive it would make an unowned question look answered.
    """
    facts = await product_facts(session, product_id)
    if facts is None:
        return []

    routed: list[Unmet] = []
    for need in needs:
        if need not in facts.unmet:
            # Already established since the decision was taken -- a guidelines task
            # that landed while this one queued. Waking a manager to look at something
            # that is no longer missing is how a department learns to ignore its inbox.
            log.info("attention.already_met", product_id=product_id, need=need.value)
            continue
        owner = OWNER.get(need)
        if owner is None:
            continue
        engine, kind = owner
        woken = await repository.wake_manager(
            session,
            engine=engine,
            kind=kind,
            product_id=product_id,
            asked_by=asked_by,
            because=_BECAUSE[need],
        )
        if woken:
            routed.append(need)

    unowned = [n for n in needs if n not in OWNER]
    log.info(
        "attention.routed",
        product_id=product_id,
        asked_by=asked_by,
        routed=[n.value for n in routed],
        unowned=[n.value for n in unowned],
    )
    return routed


#: Why the manager is being woken, in the words it will actually read. Stated as the
#: observation rather than as an instruction -- "nobody has decided how this should
#: look" leaves the decision open, where "write design guidelines for this product"
#: would have made it for them.
_BECAUSE: dict[Unmet, str] = {
    Unmet.AUDIENCE: "nobody has established who this product is for",
    Unmet.REQUIREMENTS: "nobody has written what this product must do",
    Unmet.DESIGN_GUIDELINES: "nobody has decided how this product should look",
    Unmet.DEPLOYMENT: "this product has never been deployed",
    Unmet.REACH: "nobody outside the company has seen this product",
}
