"""The Design manager: works out how the design work gets done, and delegates it.

The first department here that is not a fixed dispatch. Development's engine is a
table -- `if kind is BUILD: _build()`, `if kind is PATCH: _patch()` -- and that is
right for work somebody else has already decided needs doing. Design's is not like
that: *which* products need attention, in what order, and at what depth has a
different answer every day.

**What this manager does not decide is whether the work is worth doing.** It used
to, and that was the error. Every line of its brief pushed toward restraint -- "every
item you commission costs real money", "a department that always finds work is
manufacturing it", "a product nobody will ever look at is not worth reviewing" --
with nothing anywhere about the cost of not acting. On 2026-09-16 it was asked for
guidelines on a brand-new product and declined, verbatim: *"guidelines are only
worth writing for products that will be seen by real users... writing design
guidelines now would be premature."* Meanwhile the supervisor was holding the build
until a standard existed. Both were reasonable; the product stopped dead between
them.

Jesse's correction, and it is the right model: *"a designer that gets paid for their
work doesn't care if it's worth the money, they do the work. A department manager
just makes sure the work gets done. Only the CEO cares about the money part."* So
cost reasoning lives in exactly two places now -- the spending limit, which is a rail
enforced in code, and whoever decides what the company builds. An employee running a
shadow copy of the budget in its own head does worse work and vetoes things that were
already decided.

Deterministic code still turns the decision into tasks: the manager chooses what
happens, the system records it, and a manager that could act directly could spend in
ways nothing captures.

**Freedom and rails, stated plainly**, because the balance is the whole design:

- Free to choose what matters, in what order, and at what depth.
- Free to say the standard itself is wrong. Several products drawing the same
  complaint is evidence about the house style, not about any one of them, and
  noticing that is the judgement this role exists for.
- Free to ask, rather than guess, through the same channel every other department
  uses.
- Free to commission nothing on an ordinary sweep where nothing has changed -- which
  is a different thing from declining something it was asked for.

- Railed so it cannot block a release. Design findings are weighed by the release
  decision like any other input; a department that could veto on taste would
  recreate the unbounded-findings gate the release judgement exists to escape.
- Railed so it cannot rewrite the house style unilaterally. A brand is a business
  decision, so the strongest move available is raising the concern to a human.
- Railed by a cap on how much it can commission at once, which bounds cost in code
  rather than asking the manager to worry about it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory import comms
from factory.handbook import brief_products
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import DesignDecision, InterruptReason, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

MANAGER_SYSTEM = f"""\
You run the design department of a small software company. You look at what your \
department is responsible for and make sure the design work gets done.

You do not do the work yourself. You decide *what happens and in what order*, and \
somebody else executes it.

**Your job is to get the work done, not to decide whether it is worth doing.** \
Nobody here needs you to weigh what a piece of work costs -- the company decides \
what it wants built and there is a hard spending limit it cannot exceed, neither of \
which is your department's problem. A designer who second-guesses whether a brief \
deserves their time does worse work than one who simply does it well. When you are \
asked for something, the question in front of you is how to do it properly.

What you can commission:

- **write_guidelines** -- nobody has written down how a product should look. A \
product that has not shipped yet is the *most* useful time for this, not the least: \
for a validation page the look is the product, since it is the only thing a stranger \
has to judge whether these people understand their problem.
- **review_product** -- look at a deployed page and judge it against its \
guidelines.
- **raise_house_style_concern** -- the standard itself is wrong. Use this when \
several products draw the same complaint: that is evidence about the house style, \
not about any one of them. It goes to a person, because a brand is a business \
decision and not yours to overwrite.

Judging what matters:

- One product with an odd finding is a product problem. Three with the same \
finding is a standard problem, and fixing them one at a time is how you stay busy \
forever without improving anything.
- On an ordinary sweep with nothing new in front of you and every product already \
covered, commissioning nothing is a fine answer. That is different from being asked \
for something specific, which you do.

What you could not establish goes in `needs_to_know`, and it is not one of the \
things you commission -- you can ask and still commission the work you were sure \
about. Asking beats stalling, and both beat guessing.

{comms.ASKING_GUIDANCE}

Write your assessment first, then what follows from it -- not the other way round.\
"""


def _subject_line(subject: repository.DesignSubject) -> str:
    """One product, with the two facts that change what is worth doing to it.

    Its status and whether a page exists are stated rather than filtered on. A
    product still being built cannot be reviewed -- there is nothing to look at --
    but it is the best moment to write its guidelines, because the builder is about
    to need them. Which of those matters is the manager's call, so it gets the facts
    rather than a shortlist somebody else narrowed for it.
    """
    page = "a page is deployed" if subject.deployed else "nothing deployed to look at yet"
    return f"  - {subject.id}  {subject.name}\n      status: {subject.status}, {page}"


def _render_domain(domain: repository.DesignDomain) -> str:
    """The department's state as the manager sees it: rows, never recollection."""
    lines: list[str] = []

    # An empty set has two opposite readings: every product is covered, or there are
    # no products. Rendering both as "Every product has design guidelines" hands the
    # manager three green lights for a department that may not contain a single
    # product -- vacuously true, and indistinguishable from good health. Say which.
    nothing_in_scope = not domain.without_guidelines and not domain.unreviewed

    if domain.without_guidelines:
        lines.append("Products with no design guidelines written for them:")
        lines += [_subject_line(s) for s in domain.without_guidelines[:10]]
    elif nothing_in_scope:
        lines.append(
            "There are no products in your scope at all -- not 'all covered', but "
            "nothing to cover. Either every product is retired, or none exists yet."
        )
    else:
        lines.append("Every product has design guidelines.")

    if domain.unreviewed:
        lines.append("\nHave guidelines, never reviewed against what actually shipped:")
        lines += [_subject_line(s) for s in domain.unreviewed[:10]]
    elif not nothing_in_scope:
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


def _subject(task: Task) -> str:
    """Why this tick is running, when something specific prompted it.

    An empty string for the periodic sweep, which runs because it is a new day and
    has no subject. When the department is woken through its inbox
    (`supervisor/attention.py`), this is the assignment.

    The previous version ended *"that is an observation, not an instruction -- decide
    for yourself whether anything is owed here"*, which invited the department to
    decline work the company had already decided it wanted. It did, and the product
    deadlocked. Judgement about **how** to do the work is real and stays here;
    judgement about **whether** belongs to whoever decides what gets built.
    """
    payload = task.payload or {}
    product_id = payload.get("product_id")
    because = payload.get("because")
    if not product_id or not because:
        return ""
    asked_by = payload.get("asked_by") or "another department"
    return (
        f"**You are being asked for design work on product {product_id}**, by "
        f"{asked_by}, because {because}. Somebody has already decided this is wanted, "
        "so it is yours to carry out rather than to reconsider -- what is yours is how "
        "to do it well. Commission it, and say what you are having done.\n\n"
    )


async def run_manager_tick(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Look at the department, decide what matters, commission it."""
    domain = await repository.design_domain(session)

    # If this tick was parked on a question, it is running again because somebody
    # answered. Put the answer in front of the manager, or the round trip bought
    # nothing and it parks again on exactly the question it just had answered.
    answered = await comms.answers_for(session, task.id)

    # What is actually known about these products, not just their names. A manager
    # asked to judge what is worth doing, and given only a list of ids, has two
    # moves: guess or ask -- and all three of this department's first live runs did
    # exactly that, blocking on "who is this for" and "does anyone want it", both of
    # which are rows.
    #
    # `brief_products` is general and lives in the handbook for that reason: every
    # manager has this problem, so the next one inherits the fix instead of
    # rediscovering it.
    briefs = await brief_products(
        session,
        [s.id for s in domain.without_guidelines] + [s.id for s in domain.unreviewed],
    )

    try:
        decision = await ctx.llm.parse(
            DesignDecision,
            task_type=TaskType.ANALYSIS,
            system=MANAGER_SYSTEM,
            prompt=(
                (f"{answered}\n\n" if answered else "")
                + _subject(task)
                + "This is what your department looks like right now.\n\n"
                + f"{_render_domain(domain)}\n\n"
                + (f"{briefs}\n\n" if briefs else "")
                + "What, if anything, is worth doing?"
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
            "asked": [q.question[:200] for q in decision.needs_to_know],
        },
    )

    # Before the empty-actions check, because the likeliest shape of a tick with
    # questions is *no actions and one question* -- exactly what happened live. Read
    # in the other order, "I cannot establish whether this matters, so I am asking"
    # would be logged as "nothing worth doing" and the question silently dropped.
    #
    # General machinery, called identically by every department manager: this module
    # holds no comms code of its own.
    asked = await comms.raise_decision_questions(
        session, asked_by=EngineName.DESIGN, decision=decision, task_id=task.id
    )

    if not decision.actions:
        log.info("design.nothing_to_do", asked=asked, assessment=decision.assessment[:200])
        if asked:
            return f"design: asked {asked} question(s) and parked -- {decision.assessment[:150]}"
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
        asked=asked,
        assessment=decision.assessment[:200],
    )
    return f"design: commissioned {commissioned} of {len(decision.actions)} action(s)" + (
        f", asked {asked} question(s) and parked" if asked else ""
    )
