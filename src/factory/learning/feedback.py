"""What a reply leaves behind after the thing it was about is finished.

Every human reply this company has ever received was consumed and discarded. A reply
to a heal-exhaustion escalation became one guided patch on one product; a reply to a
compliance ask flipped one idea's status; a reply to a domain ask attached one
hostname. In each case the instruction was carried out perfectly and the *standard*
behind it -- the reason a person would say that, which would apply again next week on
a different product -- evaporated the moment the task closed.

The cost of that shows up as a particular kind of work. Told that a confirmation
email reading "Thanks for your interest" with no product name and no sender was
"boring and indescriptive", this company had nowhere to put the observation, so it
travelled to a human who wrote a commit. That is a functioning organisation only
while somebody is willing to keep writing the commits, and an organisation whose
standards can only arrive as commits cannot outgrow the person writing them.

**Asked of every reply, never offered as a choice.** The prior art is unambiguous:
`report_product_issue` shipped as an elected action and two full live runs went by
without a single agent electing it. A judgement that only happens when somebody
remembers to make it is one that mostly does not happen, and its absence is
indistinguishable from a considered "nothing to learn here".

**Its own task, not a step inside `resolve_reply`.** Two requirements pull apart: the
reply must act on the thing at hand immediately and without depending on a model
being reachable, and deciding what outlives it needs a model. Putting the second
inside the first would let a slow or failed extraction delay a domain attachment --
or, worse, be wrapped in a `try`/`except` that makes a skipped judgement invisible.
As a task it is queued, retried, and visible when it fails. Same posture as `_retain`
in `marketing/email.py`: what must happen happens first, what we want to keep is kept
on its own terms.

**The line this must hold.** A remembered preference may shape judgement. It may
never relax a constraint. The suppression list, erasure, the spend cap, the
campaign-approval gate and what may be claimed to a customer are rails, and a
learning write path is exactly the mechanism by which an autonomous system would
otherwise talk its way out of its own. A reply that reads as softening one is refused
and handed back to a human rather than filed -- a refusal is cheap and a quietly
learned exemption is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Interrupt, Task
from factory.state.schemas import InterruptReason, ProposedLesson, TaskKind

if TYPE_CHECKING:
    # `supervisor.context` imports `Notifier` from `hitl.interrupts`, and
    # `hitl.interrupts` imports this module to capture every reply -- so a real
    # import here closes a cycle. Same treatment `hitl/interrupts.py` already gives
    # `FactoryContext` for the same reason.
    from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Capabilities a lesson can actually be filed against -- which is to say, the ones
#: something recalls from. Not decoration: `capability:` memory is read by
#: `agents/spawner.py` and `development/preship.py`, both keyed on a `RoleName`, so a
#: lesson filed under a capability that is not one of these is never read by anything
#: and its absence looks exactly like never having been written. The model is shown
#: this list and its answer is checked against it.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset(r.value for r in RoleName)

#: Departments a lesson can be filed against, for a standard that is broader than one
#: job. Same requirement as above -- `scopes_for(engine=...)` is what makes it
#: readable.
ALLOWED_ENGINES: frozenset[str] = frozenset(e.value for e in EngineName)

SYSTEM = """\
You read one exchange between this company and a person outside it, and answer one \
question: does anything in the reply outlive the thing it was about?

Most replies do not, and saying so is the common and correct answer. "Yes, ship it", \
"use this domain", "reject that one" -- these are instructions about one situation, \
they have already been carried out, and inventing a standard out of one would put \
noise into every future agent's context that it has to read past. **When in doubt, \
say it is not durable.** A store of forty vague lessons is worse than a store of \
four sharp ones, because recall has to surface something and it will surface these.

What does outlive it is a standard: a statement about how work of this kind should \
be done, that would still make sense to somebody who never saw this product or this \
escalation. "The confirmation page a visitor lands on should look like the product \
they just signed up for, not like a system message" is a standard. "Fix the Rise \
confirmation page" is the same sentence with its usefulness removed.

Test it: write the lesson down, imagine handing it to somebody starting a different \
product next month, and ask whether they could act on it. If it only makes sense \
with this product in front of them, it is not durable.

**Scope is who carries it, and it is the safety property.** Pick the narrowest scope \
that the lesson is actually true in:

- `capability` -- a professional standard for one job, true on every product. This is \
usually the right answer for a judgement about how work should be done. Name the \
capability exactly as it appears in the list you are given.
- `engine` -- true for a whole department, across the jobs inside it.
- `product` -- true only for this one product, and would be wrong applied elsewhere.
- `company` -- true everywhere, for everyone. Rare. Use it only when no narrower \
scope would be honest, because everything filed here is read by every agent in the \
company on every run.

**One thing you must refuse.** This company has rails: the do-not-contact and \
suppression lists, data erasure on request, the spending limit, the requirement that \
a campaign is approved by a human before it sends, and the rule that it never claims \
something to a customer that is not true. These exist precisely so that no argument, \
however reasonable in the moment, can move them. If the reply asks for one to be \
loosened, set `touches_a_rail` and do not write it as a lesson -- a person will look \
at it instead. A reply that works *within* a rail does not count: "this lead is on \
the suppression list, drop it" is somebody applying a rail, not challenging one.\
"""


def _exchange(interrupt: Interrupt, action: str) -> str:
    """What the model is given: the ask, the reply, and what the reply already did.

    The last part matters more than it looks. Without it a model reads "attach
    rise-audit.com" and proposes remembering the hostname -- a lesson to repeat work
    that has already been done. Knowing the instruction was carried out is what moves
    the question from "what did they want" to "what did they mean that keeps being
    true".
    """
    capabilities = ", ".join(sorted(ALLOWED_CAPABILITIES))
    engines = ", ".join(sorted(ALLOWED_ENGINES))
    return (
        f"This company asked:\n"
        f"  {interrupt.subject}\n"
        f"  {(interrupt.detail or '').strip()[:2000]}\n\n"
        f"A person replied:\n"
        f"  {(interrupt.response or '').strip()[:2000]}\n\n"
        f"That reply has already been acted on -- the immediate outcome was: {action}. "
        f"You are not deciding what to do about it. You are deciding whether anything "
        f"in it should still be true after this is closed.\n\n"
        f"Capabilities you may name: {capabilities}\n"
        f"Departments you may name: {engines}"
    )


def _scope_for(lesson: ProposedLesson, *, product_id: str | None) -> tuple[str, str]:
    """Resolve the judged scope to a real one, and say if it had to be widened.

    A named scope that nothing reads from is worse than no scope at all, because the
    lesson exists, counts against the store's budget, and never reaches anybody. So a
    capability or department the company does not have does not silently become a
    `capability:` row -- it widens to the company scope, which is over-broad but is at
    least read. The widening is logged, since a model repeatedly naming a capability
    that does not exist is telling us something about the org chart.
    """
    if lesson.scope == "capability":
        name = lesson.capability.strip().lower()
        if name in ALLOWED_CAPABILITIES:
            return memory_store.capability_scope(name), ""
        return memory_store.GLOBAL_SCOPE, f"no such capability {name[:40]!r}"
    if lesson.scope == "engine":
        name = lesson.capability.strip().lower()
        if name in ALLOWED_ENGINES:
            return f"engine:{name}", ""
        return memory_store.GLOBAL_SCOPE, f"no such department {name[:40]!r}"
    if lesson.scope == "product":
        if product_id:
            return f"product:{product_id}", ""
        # Judged product-specific with no product in the exchange. Filing it globally
        # would apply a deliberately narrow lesson everywhere, which is the one
        # direction scope must never move by accident.
        return "", "product-scoped but the exchange names no product"
    return memory_store.GLOBAL_SCOPE, ""


async def capture(
    session: AsyncSession, *, interrupt: Interrupt, answer: str, action: str
) -> str | None:
    """Queue the question "does any of this outlive the task?" about one reply.

    Called from `resolve_reply` for every reply regardless of origin or reason. Cheap
    and synchronous -- one insert, no model -- so it cannot delay the reply taking
    effect; the judgement itself happens in the task.
    """
    if not answer.strip():
        # Somebody acknowledging an escalation without saying anything. There is no
        # standard in silence, and queueing a judgement about it spends a model call
        # to conclude exactly that -- `learn` already returns "nothing was said", but
        # only after the task exists. Found by an integration test asserting that an
        # empty reply enqueues nothing at all, which this had quietly broken.
        return None
    if interrupt.reason == InterruptReason.CONSTRAINT_CHALLENGED.value:
        # Do not learn from the refusal to learn. This reason exists only because an
        # earlier extraction declined to file something and asked a human instead;
        # running extraction on the answer invites a cycle whose every turn is an
        # escalation, and the human is already looking directly at the question.
        return None
    return await repository.enqueue_task(
        session,
        kind=TaskKind.LEARN_FROM_FEEDBACK,
        engine=EngineName.SUPERVISOR,
        # Per interrupt, and an interrupt is resolved once -- so this is a lifetime
        # key on something that genuinely happens once in a lifetime, which is the
        # only shape that use survives.
        idempotency_key=f"learn:interrupt:{interrupt.id}",
        payload={"interrupt_id": interrupt.id, "action": action},
        product_id=interrupt.product_id,
        # Behind everything that moves a product forward. Nothing waits on this, and a
        # backlog of them should never delay a build.
        priority=180,
    )


async def learn(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Decide whether this reply contained a standard, and file it if so."""
    interrupt_id = str((task.payload or {}).get("interrupt_id") or "")
    interrupt = await session.get(Interrupt, interrupt_id) if interrupt_id else None
    if interrupt is None:
        return f"learning: no such interrupt {interrupt_id}"
    if not (interrupt.response or "").strip():
        return "learning: nothing was said"

    action = str((task.payload or {}).get("action") or "none")
    lesson = await ctx.llm.parse(
        ProposedLesson,
        task_type=TaskType.ANALYSIS,
        system=SYSTEM,
        prompt=_exchange(interrupt, action),
        engine=EngineName.SUPERVISOR,
        task_id=task.id,
        max_tokens=4_000,
    )

    if lesson.touches_a_rail:
        # Deliberately not filed, not even in a weakened form. The whole value of a
        # rail is that it does not move by degrees, and "we wrote down a softened
        # version of it" is exactly how it would.
        await _refuse(session, interrupt, lesson)
        log.warning(
            "learning.rail_challenged",
            interrupt_id=interrupt.id,
            why=lesson.why[:200],
        )
        return f"learning: refused and escalated -- {lesson.why[:120]}"

    if not lesson.durable:
        log.info("learning.not_durable", interrupt_id=interrupt.id, why=lesson.why[:160])
        return f"learning: nothing durable -- {lesson.why[:120]}"

    scope, widened = _scope_for(lesson, product_id=interrupt.product_id)
    if not scope:
        log.warning("learning.unfilable", interrupt_id=interrupt.id, detail=widened)
        return f"learning: could not file this -- {widened}"
    if widened:
        log.warning("learning.scope_widened", interrupt_id=interrupt.id, detail=widened)

    memory_id = await memory_store.remember(
        session,
        content=lesson.content,
        kind=memory_store.MemoryKind.PREFERENCE,
        scope=scope,
        source_task_id=task.id,
        origin="human_reply",
        # Higher than an agent's own inference, below certainty: a person said it, and
        # what was extracted from what they said is this module's reading of it.
        confidence=0.8,
    )
    if memory_id is None:
        return "learning: the lesson was too slight to keep"
    log.info("learning.remembered", interrupt_id=interrupt.id, scope=scope, memory_id=memory_id)
    return f"learning: remembered at {scope} -- {lesson.content[:120]}"


async def _refuse(session: AsyncSession, interrupt: Interrupt, lesson: ProposedLesson) -> None:
    from factory.hitl.interrupts import request

    await request(
        session,
        reason=InterruptReason.CONSTRAINT_CHALLENGED,
        subject=f"A reply would relax a rail: {interrupt.subject[:120]}",
        detail=(
            "This reply was carried out as an instruction, and it was not kept as a "
            "standing rule, because acting on it in future would loosen one of the "
            "limits this company holds regardless of circumstance.\n\n"
            f"What was replied:\n  {(interrupt.response or '').strip()[:800]}\n\n"
            f"Read as:\n  {lesson.content[:400] or '(nothing was written down)'}\n\n"
            f"Why it was refused:\n  {lesson.why[:400]}\n\n"
            "Nothing is blocked. If the limit itself should move, that is a change to "
            "the rails and belongs in code where it can be reviewed, not in memory "
            "where it would apply quietly."
        ),
        headline="⚖️ Rule change, not a preference",
        reply_hint=(
            "No reply needed unless the limit should actually change -- in which case "
            "say so here and it will be raised as a code change, not remembered."
        ),
        needs_reply=False,
        product_id=interrupt.product_id,
    )
