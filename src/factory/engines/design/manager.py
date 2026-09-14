"""The Design manager: decides what design work is worth doing, and delegates it.

The first department here that is not a fixed dispatch. Development's engine is a
table -- `if kind is BUILD: _build()`, `if kind is PATCH: _patch()` -- and that is
right for work somebody else has already decided needs doing. Design's work is not
like that. "What design work matters right now" has a different answer every day,
and the answer is often *none*.

So this tick does one thing: look at the department's own domain and decide. It
performs no work itself. Deterministic code turns its decision into tasks, which
is the split that keeps the judgement real and the outcome checkable -- the
manager chooses *what is worth doing*, the system decides *what happens*, and a
manager that could act directly would be able to spend money in ways nothing
records.

**Freedom and rails, stated plainly**, because the balance is the whole design:

- Free to decide *nothing needs doing*, and that is a correct answer rather than a
  failed tick. A department that always finds work is manufacturing it, and every
  item it commissions costs real money.
- Free to choose what matters, in what order, and to leave things it judges not
  worth the spend -- including a product nobody has ever looked at.
- Free to say the standard itself is wrong. Several products drawing the same
  complaint is evidence about the house style, not about any one of them, and
  noticing that is the judgement this role exists for.
- Free to ask, rather than guess, through the same channel every other department
  uses.

- Railed so it cannot block a release. Design findings are weighed by the release
  decision like any other input; a department that could veto on taste would
  recreate the unbounded-findings gate the release judgement exists to escape.
- Railed so it cannot rewrite the house style unilaterally. A brand is a business
  decision, so the strongest move available is raising the concern to a human.
- Railed by a cap on how much it can commission at once, which bounds cost without
  deciding anything for it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import DesignDecision, InterruptReason, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

MANAGER_SYSTEM = """\
You run the design department of a small software company. Once a day you look at \
what your department is responsible for and decide what, if anything, is worth \
doing.

You do not do the work. You decide what work happens, and somebody else does it -- \
so what you are judging is *what deserves attention*, not how to execute it.

**Doing nothing is a real decision and often the right one.** Every item you \
commission costs money. A department that finds work every day is manufacturing \
it. If the products in front of you are fine and nothing has changed, say so and \
commission nothing.

What you can commission:

- **write_guidelines** -- a product is live and nobody ever wrote down how it \
should look. Worth doing when that product still matters; not worth doing for \
something about to be retired.
- **review_product** -- look at a deployed page and judge it against its \
guidelines. Costs a real model call with a screenshot, so it earns its place when \
the product is worth the attention or something suggests it went wrong.
- **raise_house_style_concern** -- the standard itself is wrong. Use this when \
several products draw the same complaint: that is evidence about the house style, \
not about any one of them. It goes to a person, because a brand is a business \
decision and not yours to overwrite.

Judging what matters:

- A product nobody will ever look at is not worth reviewing however imperfect it \
is. Spend attention where a real visitor will see the result.
- One product with an odd finding is a product problem. Three with the same \
finding is a standard problem, and fixing them one at a time is how you stay busy \
forever without improving anything.
- If something genuinely blocks you and another department would know, ask rather \
than guess. Your work parks and resumes with the answer.

Write your assessment first, then the actions that follow from it -- not the other \
way round.\
"""


def _render_domain(domain: repository.DesignDomain) -> str:
    """The department's state as the manager sees it: rows, never recollection."""
    lines: list[str] = []

    if domain.without_guidelines:
        lines.append("Live products with no design guidelines written for them:")
        lines += [f"  - {pid}  {name}" for pid, name in domain.without_guidelines[:10]]
    else:
        lines.append("Every live product has design guidelines.")

    if domain.unreviewed:
        lines.append("\nHave guidelines, never reviewed against what actually shipped:")
        lines += [f"  - {pid}  {name}" for pid, name in domain.unreviewed[:10]]
    else:
        lines.append("\nEvery product with guidelines has been reviewed at least once.")

    if domain.open_findings:
        lines.append("\nDesign findings still open, newest first:")
        lines += [f"  - [{pid[:8]}] {title[:110]}" for pid, title in domain.open_findings[:12]]
        lines.append(
            "\n(Look for the same complaint across different products -- that is the "
            "house style being wrong, not three separate mistakes.)"
        )
    else:
        lines.append("\nNo open design findings.")

    if domain.open_questions:
        lines.append(
            f"\n{domain.open_questions} question(s) are waiting for this department. "
            "They are answered as their own work; you do not need to commission that."
        )
    return "\n".join(lines)


async def run_manager_tick(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Look at the department, decide what matters, commission it."""
    domain = await repository.design_domain(session)

    try:
        decision = await ctx.llm.parse(
            DesignDecision,
            task_type=TaskType.ANALYSIS,
            system=MANAGER_SYSTEM,
            prompt=(
                "This is what your department looks like right now.\n\n"
                f"{_render_domain(domain)}\n\n"
                "What, if anything, is worth doing?"
            ),
            engine=EngineName.DESIGN,
            task_id=task.id,
            max_tokens=16_000,
        )
    except Exception as exc:
        # A manager that could not think is not a manager that decided nothing.
        # Failing the tick leaves the department exactly as it was, which is the
        # honest outcome -- inventing a default decision here would commission work
        # nobody chose.
        log.warning("design.manager_failed", detail=str(exc)[:200])
        return f"design: manager could not decide -- {str(exc)[:150]}"

    await repository.record_task_event(
        session,
        event="design.decided",
        task_id=task.id,
        product_id=None,
        engine=EngineName.DESIGN,
        detail={
            "assessment": decision.assessment[:1000],
            "actions": [
                {"kind": a.kind, "product_id": a.product_id, "why": a.why[:200]}
                for a in decision.actions
            ],
        },
    )

    if not decision.actions:
        log.info("design.nothing_to_do", assessment=decision.assessment[:200])
        return f"design: nothing worth doing -- {decision.assessment[:200]}"

    commissioned = 0
    for action in decision.actions:
        if action.kind == "raise_house_style_concern":
            # The one thing it may not do itself. A brand is a business decision,
            # so the strongest available move is to make the case to a person.
            await interrupts.request(
                session,
                reason=InterruptReason.DATA_INTEGRITY,
                subject="Design thinks the house style is wrong",
                detail=(
                    f"{decision.assessment}\n\nWhat prompted it:\n{action.why}\n\n"
                    "The house style lives in `requirements.py::HOUSE_STYLE`. Design "
                    "cannot change it -- a brand is yours to decide."
                ),
                headline="🎨 Design: the house style may be the problem",
                reply_hint="No reply needed. Change the house style if you agree.",
                needs_reply=False,
            )
            commissioned += 1
            continue

        if not action.product_id:
            log.info("design.action_without_product", kind=action.kind)
            continue

        kind = (
            TaskKind.WRITE_DESIGN_GUIDELINES
            if action.kind == "write_guidelines"
            else TaskKind.REVIEW_PRODUCT_DESIGN
        )
        task_id = await repository.enqueue_task(
            session,
            kind=kind,
            engine=EngineName.DESIGN,
            # Scoped to the decision that made it, so the same product can be
            # revisited on a later tick when the manager judges it worth doing
            # again -- a lifetime-unique key would make "review this again" silently
            # impossible.
            idempotency_key=f"design:{action.kind}:{action.product_id}:{task.id}",
            payload={"product_id": action.product_id, "why": action.why},
            product_id=action.product_id,
            priority=70,
        )
        if task_id is not None:
            commissioned += 1

    log.info(
        "design.commissioned",
        actions=len(decision.actions),
        commissioned=commissioned,
        assessment=decision.assessment[:200],
    )
    return f"design: commissioned {commissioned} of {len(decision.actions)} action(s)"
