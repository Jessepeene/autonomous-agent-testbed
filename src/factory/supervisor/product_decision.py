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

from factory.facts import ENDS, OWNER, ProductFacts, Unmet, product_facts
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
You decide what one product does next. You are the only part of this company that \
looks at a product as a whole rather than at its own department's slice of it.

You are given the product's goal, what it has so far, and what can actually be \
started right now. **Your question is "what is the next thing worth doing to move \
this toward its goal?"** -- not "what is this product missing?". Those are different \
questions and the second one has no answer: a product in progress is missing most of \
what it will eventually have, and a list of absences tells you nothing about which \
way is forward.

Do not weigh what work costs. There is a hard spending limit enforced elsewhere that \
nothing you decide can exceed, so the money question is already answered and running \
a second copy of it here only makes you timid.

Two failures to avoid, and the second is by far the more common:

- Starting everything at once. Work started before its inputs exist gets done twice.
- Starting nothing because starting something feels premature. A product sitting with \
nothing in flight is not being careful, it is stalled -- and nobody is coming to \
notice.

Think about what consumes what. Some work takes another piece of work as its input, \
and starting that first means doing it blind and then doing it again. That is a \
judgement about this product, not a rule: sometimes the generic answer really is good \
enough and waiting buys nothing. Say which case this is.

The goal is never an obstacle. If the product's purpose is to find out whether people \
want it, then not yet knowing is the reason to act, not a reason to wait -- you cannot \
learn it by waiting, only by putting something in front of somebody.

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
    """The goal, then what can actually be started toward it.

    This used to be a list of everything the product lacked, split by who owned what,
    and it stalled four runs. A flat list of absences has no direction in it: the
    decider has to reconstruct which entries are prerequisites, which are byproducts
    of other work, and which are the objective -- and it got that wrong differently
    every run, reaching three different orderings from near-identical facts.

    Jesse's question is what named it: *how would a human team solve this, or how
    would it not come up?* A human team never asks "what is missing". They start from
    the goal, step back once, and do the first thing. So this says what the product is
    for, and then what is startable now -- ends in the goal, not the gap list, and
    nothing here that nobody can act on.
    """
    startable = [n for n in facts.unmet if _pursuable(n) and n not in ENDS]
    byproduct = [n for n in facts.unmet if n in _PRODUCED_BY]

    lines = [_goal(facts), ""]
    if startable:
        lines.append("What can be started right now:")
        lines.extend(f"  - {n.value}: {_WHAT[n]}" for n in startable)
    else:
        lines.append(
            "Nothing new can be started right now; whatever is already in flight is "
            "what moves this forward."
        )

    # Requirements are written by the builder as the first step of a build, so "no
    # requirements" is not a reason to hold the build -- it is a description of what
    # the build has not done yet. Listed as a gap, it read as a blocker nobody could
    # clear, and two runs concluded the product could not start.
    if byproduct:
        lines.append("")
        lines.append("Comes with other work rather than being started on its own:")
        lines.extend(f"  - {n.value}: {_PRODUCED_BY[n]}" for n in byproduct)

    # Needs nobody owns are deliberately absent from this prompt. They are a fact about
    # the company's staffing, not about this product, and a decider shown "you cannot
    # pursue these" reasonably concludes the product is stuck -- which is what happened
    # on runs 8 and 10. They are raised to a human instead, once, by `_raise_staffing_gap`.
    return "\n".join(lines)


def _goal(facts: ProductFacts) -> str:
    """What this product exists to find out, stated as the objective it is.

    `reach` and `interest` used to appear in the gap list, which made the point of the
    exercise look like an obstacle to it. Stated here instead: not yet knowing whether
    anyone wants this is the *reason* to act.
    """
    measured = (
        f"So far {facts.interest} of them have said yes, out of {facts.reach} approached."
        if facts.reach
        else "Nobody has been approached yet, so there is no answer either way -- and "
        "there cannot be one until something exists to put in front of them."
    )
    return (
        f"**What this product is for.** It exists to find out whether "
        f"{facts.audience_sentence} will pay for it. That question is the goal; "
        f"everything below is only useful insofar as it gets the company an answer.\n"
        f"{measured}"
    )


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
    # States what a build reads and what it writes, because the decider has to know
    # that to reason about order at all. Run 11 concluded the design guidelines "are
    # inputs that the builder will generate as part of building" -- true of the
    # acceptance criteria, false of the design standard, which a build *reads* and
    # silently falls back to the generic house style when it is absent. On that
    # conflation it commissioned the build alongside the design work instead of after
    # it, and the page came out generic.
    #
    # This is a fact about what consumes what, not a priority: "build only after design"
    # stays a judgement, and a house style that genuinely suits a product is a perfectly
    # good reason to decide the other way.
    Unmet.DEPLOYMENT: (
        "nothing has been built or deployed, so there is nothing to put in front of "
        "anyone. A build writes its own acceptance criteria first, then generates the "
        "page -- and it *reads* the design standard rather than producing one, falling "
        "back to the generic house style if none has been decided yet"
    ),
    Unmet.REACH: "nobody outside the company has seen it",
}


async def _raise_staffing_gap(session: AsyncSession, facts: ProductFacts) -> list[Unmet]:
    """A need no department can establish is a staffing question, not a product one.

    In a company, "nobody owns this" goes to whoever does the hiring; it does not stop
    today's work. Putting it in front of the decider as something it could not pursue
    is what produced *"the product is blocked on work that nobody in this company
    currently owns"* -- true, and the wrong conclusion, because a designer does not
    down tools over the lack of a data analyst.

    Deduplicated by subject, so a capability the company lacks produces one open
    escalation however many products run into it. Returns what was raised, for the
    decision record.
    """
    from factory.hitl import interrupts
    from factory.state.schemas import InterruptReason

    # `ENDS` excluded, and the first version of this function forgot to: it would have
    # escalated "nobody owns reach", which is the same category error one layer along.
    # Reach is what the company is trying to achieve, so nobody owning it is not a
    # staffing hole -- it is the definition of the objective.
    gaps = [n for n in facts.unmet if not _pursuable(n) and n not in _PRODUCED_BY and n not in ENDS]
    for need in gaps:
        await interrupts.request(
            session,
            reason=InterruptReason.DATA_INTEGRITY,
            subject=f"Nobody owns {need.value}",
            detail=(
                f"Products keep needing this and no department can establish it: "
                f"{_WHAT[need]}.\n\n"
                "This is not blocking any one product -- work continues on everything "
                "that can be started -- but every product reaching this point is built "
                "without it, and nothing in the company is accountable for changing "
                "that. Deciding who owns it is a staffing question, which is yours."
            ),
            headline="🪑 A question nobody here owns",
            reply_hint="No reply needed — this is about who works here, not about one product.",
            needs_reply=False,
        )
    return gaps


async def _commission(
    session: AsyncSession, facts: ProductFacts, needs: list[Unmet], decision_task_id: str
) -> tuple[list[Unmet], list[Unmet]]:
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
        return routed, []

    # Two builds racing on one workspace is the thing to prevent -- not a second build
    # ever. This key used to be `validate:{idea_id}`, scoped to the idea's lifetime, so
    # a product was buildable exactly once: when run 14's build failed its review and
    # the supervisor decided to rebuild, `enqueue_task` returned None, nothing
    # happened, and the product stalled forever while the log said it was holding for
    # design. A decision recorded and then discarded, which is the defect shape this
    # module was written to remove.
    if await repository.has_unfinished_task(
        session, product_id=facts.product_id, kind=TaskKind.BUILD_LANDING_PAGE
    ):
        return routed, [Unmet.DEPLOYMENT]

    created = await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_LANDING_PAGE,
        engine=EngineName.DEVELOPMENT,
        # Scoped to the decision that asked for it, so one decision retried cannot
        # double-build while a later decision still can build at all.
        idempotency_key=f"validate:{facts.product_id}:{decision_task_id}",
        payload={"idea_id": facts.idea_id, "product_id": facts.product_id},
        idea_id=facts.idea_id,
        product_id=facts.product_id,
        priority=50,
    )
    if created is None:
        return routed, [Unmet.DEPLOYMENT]
    routed.append(Unmet.DEPLOYMENT)
    return routed, []


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
                "What is the next thing worth doing to move this toward its goal?"
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

    routed, blocked = await _commission(session, facts, wanted, task.id)
    if blocked:
        # A decision that could not be carried out is not a decision to do nothing, and
        # the two used to produce the same line. Run 14's supervisor chose to rebuild,
        # the commission was swallowed by an idempotency key, and the note read
        # "holding because..." -- so a permanently stuck product looked like a
        # deliberate pause for design, and stayed that way.
        log.warning(
            "supervisor.commission_blocked",
            product_id=product_id,
            blocked=[n.value for n in blocked],
            reason="work of this kind is already in flight for this product",
        )
    # Raised after the decision, never before it: what the company is not staffed for
    # must not reach the decider as something it failed to pursue.
    staffing = await _raise_staffing_gap(session, facts)
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
            "staffing_gaps": [n.value for n in staffing],
            "blocked": [n.value for n in blocked],
            "unmet": [n.value for n in facts.unmet],
        },
    )
    if not routed:
        # Which of the two it was. "Chose to start nothing" and "could not start what
        # it chose" are different facts about the company and must not read alike.
        if blocked:
            return (
                f"supervisor: {facts.name} -- chose "
                f"{', '.join(n.value for n in blocked)} but it is already in flight"
            )
        return f"supervisor: {facts.name} -- nothing new commissioned" + (
            f"; holding because {decision.holding[:120]}" if decision.holding else ""
        )
    return f"supervisor: {facts.name} -- commissioned {', '.join(n.value for n in routed)}"
