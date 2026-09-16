"""What is this product owed next? The judgement that used to be a transaction.

Compliance approved an idea, created the product, and commissioned a landing-page
build in the same `session_scope`. Nothing between those lines asked whether the
product was ready to be built -- so a page was built for an audience nobody had
established, to a standard nobody had written, and was then failed by a reviewer for
not meeting that standard. Five of the seven things that go wrong in this system have
that shape: not a bad decision, a decision nobody was asked to make.

**Ordering is a conclusion here, never a rule.** Jesse, on an earlier version of this
that put the design wake at priority 40 and the build at 50: *"the system should be
smart enough to know design needs to finish before dev without us needing to hard code
it."* He is right, and the priority number was a rule wearing a number's clothes.
Nothing in this module says design precedes development. What a decider can see is that
the builder consumes design guidelines, that none exist, and that building now means
building blind and paying for it twice -- and for a product whose house style genuinely
suits it, the same facts support the opposite answer. A dependency graph cannot tell
those two apart. A judgement can.

**The rails, named as rails.** It may only pursue needs the company has an owner for,
it commissions nothing directly, and it cannot invent a need that `ProductFacts` does
not report. Everything else is its call, including "nothing yet".
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.facts import OWNER, ProductFacts, Unmet, product_facts
from factory.handbook import product_brief
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import ProductNext, TaskKind
from factory.supervisor.attention import route_attention
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

SYSTEM = """\
You decide what one product is owed next. You are the only part of this company that \
looks at a product as a whole rather than at its own department's slice of it.

You do not do the work and you do not say how it should be done. You decide which of \
the things this product is missing are worth pursuing *now*, and the rest wait.

Do not weigh what work costs. There is a hard spending limit enforced elsewhere that \
nothing you decide can exceed, so the money question is already answered and running \
a second copy of it here only makes you timid.

Two failures to avoid, and the second is by far the more common:

- Starting everything at once. A product missing five things does not need five jobs \
opened this minute, because work done before its inputs exist gets done twice.
- Starting nothing because starting something feels premature. A product sitting with \
nothing in flight is not being careful, it is stalled -- and nobody else is coming to \
notice.

Think about what consumes what. Some work takes another piece of work as its input, \
and starting it first means doing it blind and then doing it again. That is a \
judgement about this product, not a rule: sometimes the generic answer really is good \
enough and waiting buys nothing. Say which case this is.

Finding that nothing new is worth starting is a complete answer when work is already \
in flight. Say so plainly rather than manufacturing a job to look decisive."""


def _pursuable(need: Unmet) -> bool:
    """Whether this company can actually act on a need, by any route.

    The single answer to that question, because holding two of them cost a run.
    `_render_options` used to build the menu from `OWNER` alone while `_commission`
    separately knew how to start a build, so `DEPLOYMENT` was absent from the map and
    the decider was told nobody could deploy -- while the code would have done it
    happily if asked. On 2026-09-16 it read that menu, concluded *"three missing
    prerequisites that have nobody accountable for them"*, and pursued nothing.

    Not a reasoning failure: the decision was correct given what it was shown, and
    what it was shown was wrong. Two derivations of one question, which is the defect
    shape this codebase keeps finding in itself.
    """
    return need in OWNER or need is Unmet.DEPLOYMENT


def _render_options(facts: ProductFacts) -> str:
    """What this product is missing, split by whether anyone can actually do it.

    An unowned need is shown rather than hidden. A decider that cannot see "nobody
    owns knowing who this is for" will quietly reason as though somebody does, which
    is the failure this whole line of work exists to stop -- and naming it is how the
    decision can say somebody should own it.
    """
    lines: list[str] = []
    ownable = [n for n in facts.unmet if _pursuable(n)]
    byproduct = [n for n in facts.unmet if n in _PRODUCED_BY]
    unowned = [n for n in facts.unmet if not _pursuable(n) and n not in _PRODUCED_BY]

    if ownable:
        lines.append("Missing, and this company has somebody who can establish it:")
        lines.extend(f"  - {n.value}: {_WHAT[n]}" for n in ownable)
    else:
        lines.append("Nothing this product is missing has an owner who could act on it.")

    # The category that did not exist, and its absence stalled two runs. Requirements
    # are written by the builder as the first step of a build, so "no requirements" is
    # not a reason to hold the build -- it is a description of what the build has not
    # done yet. Listed among the unowned, it read as a blocker nobody could clear, and
    # deciders concluded the product could not start. Stated plainly it is the
    # opposite: the way to get these is to commission the work that produces them.
    if byproduct:
        lines.append("")
        lines.append(
            "Missing, and **not separately commissionable** -- these get written as "
            "part of other work, so they are never a reason to hold that work back:"
        )
        lines.extend(f"  - {n.value}: {_PRODUCED_BY[n]}" for n in byproduct)

    if unowned:
        lines.append("")
        lines.append(
            "Missing, and **nobody owns it** -- you cannot pursue these, and saying so "
            "is how this company finds out it needs somebody to:"
        )
        lines.extend(f"  - {n.value}: {_WHAT[n]}" for n in unowned)
    return "\n".join(lines)


#: Needs that no department is commissioned for because another piece of work produces
#: them. Stated in terms of what establishes them, so a decider reads them as progress
#: pending rather than as a wall.
_PRODUCED_BY: dict[Unmet, str] = {
    Unmet.REQUIREMENTS: (
        "written by the builder as the very first step of a build, from the idea and "
        "the design standard -- so the way to get acceptance criteria is to commission "
        "the build, not to wait for them before commissioning it"
    ),
}


#: What each need actually means, in the decider's terms rather than the schema's.
_WHAT: dict[Unmet, str] = {
    Unmet.AUDIENCE: (
        "nobody has established who this is for beyond one inferred sentence carried "
        "from the original idea"
    ),
    Unmet.REQUIREMENTS: "nobody has written what this product must do",
    Unmet.DESIGN_GUIDELINES: (
        "nobody has decided how it should look; a builder starting now would use the "
        "generic house style, which is instructions to decide rather than a decision"
    ),
    Unmet.DEPLOYMENT: "nothing has been built or deployed, so there is nothing to look at",
    Unmet.REACH: "nobody outside the company has seen it",
}


async def _commission(
    session: AsyncSession, facts: ProductFacts, needs: list[Unmet]
) -> list[Unmet]:
    """Turn a decision into work, and nowhere else does this happen.

    Everything with a manager goes through `route_attention`, which asks rather than
    instructs. `DEPLOYMENT` is the exception and it is written here rather than folded
    into `OWNER` so that it stays visible: **Development has no manager**, so this is
    the one place in the company where one department is handed its work directly
    instead of being asked to look at something.

    That asymmetry is the argument for Phase 3, not a thing to tidy away. The right
    shape is a Development manager deciding *how* to build -- MVP or validation page,
    now or after the standard lands -- exactly as Design decides whether guidelines are
    owed. Until it exists, a build is commissioned, and the honest thing is to say so
    in the place it happens.
    """
    routed = await route_attention(session, facts.product_id, needs=needs, asked_by="supervisor")
    if Unmet.DEPLOYMENT not in needs:
        return routed

    # The key Compliance used to use, kept deliberately: a decision taken twice about
    # the same product must not produce two builds.
    created = await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_LANDING_PAGE,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"validate:{facts.idea_id}",
        payload={"idea_id": facts.idea_id, "product_id": facts.product_id},
        idea_id=facts.idea_id,
        product_id=facts.product_id,
        priority=50,
    )
    if created is not None:
        routed.append(Unmet.DEPLOYMENT)
    return routed


async def decide_next(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Judge what this product is owed, and commission only that."""
    product_id = str((task.payload or {}).get("product_id") or task.product_id or "")
    facts = await product_facts(session, product_id)
    if facts is None:
        return f"supervisor: no such product {product_id}"
    if not facts.unmet:
        return f"supervisor: {facts.name} is missing nothing; no decision needed"

    try:
        decision = await ctx.llm.parse(
            ProductNext,
            task_type=TaskType.ANALYSIS,
            system=SYSTEM,
            prompt=(
                f"{await product_brief(session, product_id)}\n\n"
                f"{_render_options(facts)}\n\n"
                "What is this product owed next?"
            ),
            engine=EngineName.SUPERVISOR,
            task_id=task.id,
            max_tokens=8_000,
        )
    except Exception as exc:
        # A decider that could not think has not decided nothing. Failing the task
        # leaves the product exactly as it was and the next tick asks again, which is
        # the honest outcome -- a default here would commission work nobody chose, and
        # "build it anyway" is precisely the default that caused all of this.
        log.warning(
            "supervisor.product_decision_failed", product_id=product_id, detail=str(exc)[:200]
        )
        return f"supervisor: could not decide for {facts.name} -- {str(exc)[:150]}"

    wanted: list[Unmet] = []
    for raw in decision.pursue:
        try:
            wanted.append(Unmet(raw))
        except ValueError:
            # Named something that is not a need. Dropped rather than guessed at: a
            # decision that cannot be executed as written should shrink, not be
            # reinterpreted into whatever is nearest.
            log.warning("supervisor.unknown_need", product_id=product_id, named=raw[:60])

    routed = await _commission(session, facts, wanted)
    await repository.record_task_event(
        session,
        event="supervisor.product_decided",
        task_id=task.id,
        product_id=product_id,
        engine=EngineName.SUPERVISOR,
        detail={
            "assessment": decision.assessment[:1000],
            "pursue": [n.value for n in wanted],
            "routed": [n.value for n in routed],
            "holding": decision.holding[:500],
            "unmet": [n.value for n in facts.unmet],
        },
    )
    if not routed:
        return f"supervisor: {facts.name} -- nothing new commissioned" + (
            f"; holding because {decision.holding[:120]}" if decision.holding else ""
        )
    return f"supervisor: {facts.name} -- commissioned {', '.join(n.value for n in routed)}"
