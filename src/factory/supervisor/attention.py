"""Telling a department there is something it might own. Not telling it what to do.

Design has never had an inbox. Every task it has ever run was enqueued by its own
24-hour tick, so the only way anything reached it was for it to wake on a timer and go
looking. On 2026-09-15 that produced the sharpest version of the failure: its manager
ran at 00:00:07, could see a product with no guidelines, and correctly commissioned
nothing -- the page had been built, reviewed, repaired and closed 94 minutes earlier,
and guidelines written for a shipped page are documentation, not direction. Its
judgement was right and its position in time was wrong, which is the one failure mode
that cannot be fixed by making the department better at its job.

**Routing attention is mechanical; deciding what to do is judgement.** That line is the
whole design, and it is what keeps this from being a resolver. An unmet fact wakes the
manager that owns it, with the subject named -- and that is the end of this module's
authority. The manager then sees the product like any other, and may decide nothing is
owed; "the house style is right for this one" is a legitimate and expected answer. What
this module must never do is commission the work itself, because a mapping from unmet
fact straight to work is the conveyor rebuilt with the ordering hidden in a dependency
graph instead of written down.

The same property makes the caller side safe: `route_attention` takes a product and
names no department. `OWNER` in `factory.facts` names them. So Compliance calling this
after an approval is not a Compliance-to-Design edge -- Compliance does not know Design
exists, and adding a sixth department would not touch it.

**What this does not solve, stated plainly.** A build is still commissioned by
Compliance in the same transaction that creates the product, so waking Design at the
same moment is a race rather than an order. Until something judges what a product is
owed *before* anything is commissioned -- Phase 3 -- this narrows the window from a day
to seconds without closing it. The honest measure of this module is whether Design
starts producing guidelines at all, not whether they always arrive first.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.facts import OWNER, Unmet, product_facts
from factory.logging import get_logger
from factory.state import repository

log = get_logger(__name__)


async def route_attention(session: AsyncSession, product_id: str, *, asked_by: str) -> list[Unmet]:
    """Wake the manager of every unmet need this product has an owner for.

    Returns the needs actually routed, so a caller can log what it set in motion
    without re-deriving it. Needs with no owner are returned by neither path and are
    not an error: `Unmet.AUDIENCE` has no department today, and inventing one to
    receive it would make an unowned question look answered.
    """
    facts = await product_facts(session, product_id)
    if facts is None:
        return []

    routed: list[Unmet] = []
    for need in facts.unmet:
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

    unowned = [n for n in facts.unmet if n not in OWNER]
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
