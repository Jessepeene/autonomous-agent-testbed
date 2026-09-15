"""Asking, as a state of the work rather than an action an agent elects to take.

Jesse's question, and it decided the design: *"should it be a tool? Shouldn't comms
be inherent to the system?"*

It should not be a tool, and this codebase already paid to learn why.
`report_product_issue` shipped in AUDITOR's own registry, and both live runs "spent
their whole step budget investigating and never chose to call it, so a genuine
review was performed and then discarded". An agent must remember to call a tool,
while competing for steps against the work itself -- and the agent most in need of
asking something is the confused one, least likely to spend a step on it.

So the two halves are shaped differently, and neither is a tool:

**Asking is a state.** Every repair agent is already required to end with
``RESULT: FIXED | NOT_FIXED | NOT_NEEDED | BLOCKED``, and `runtime.py` already
parses it. Being blocked is something the work *is*, expressed in a contract the
agent cannot skip. This module gives that state somewhere to go, which it did not
have: `is_blocked` was parsed, logged, and branched on by nothing, so an agent
that said it could not proceed was handed another retry.

**Answering is work.** A question addressed to a department becomes a task in that
department's queue, picked up like anything else. Nobody has to notice it, and the
answer is durable and attributable without anyone choosing to log it.

Almost none of this is new machinery. `defer_task` already lets work wait without
counting as failed -- its docstring describes this exact case -- `interrupts` is
already an addressed question with a reply, and `resolve_reply` already resumes
work an answer unblocks. What was missing is that the recipient could only ever be
a person.

**This is the company's channel, not any one department's.** The first version was
not: the Design manager held its own routing, its own delivery call and its own
park, so Marketing would have had to write all three again and get all three
subtly right. Everything general lives here, and a department reaches it two ways
depending on what it is doing:

- **Doing work** -- an agent ends with `RESULT: BLOCKED` and its question in prose.
  `raise_blocked_question` parses it. The agent needs no vocabulary beyond the
  result contract it already has.
- **Deciding** -- a manager returns a `ManagerDecision`, whose `needs_to_know` is a
  field rather than an action, so asking never competes with real work for a slot.
  `raise_decision_questions` handles all of it, and `ASKING_GUIDANCE` is the prompt
  paragraph that goes with it.

Both funnel into `ask` -> `route` -> `deliver`, so a new department gets routing to
a person on ambiguity, delivery to either a queue or a human, and a parked-not-
failed tick without writing any of it. Resuming is `answers_for`; parking is
`park_until_answered`. A department that open-codes any of this has re-introduced
the bug this module exists to remove.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Question
from factory.state.schemas import InterruptReason, ManagerDecision, TaskKind

log = get_logger(__name__)

#: How long a question is given before the work waiting on it is retried anyway.
#: Not a deadline on the answer -- an unanswered question stays open and keeps
#: blocking. This is the rail that stops one unanswerable question stranding a
#: product forever, and it is deliberately long enough that a real answer arrives
#: first.
BLOCKED_RETRY_HOURS = 6

#: What a department can be asked about. Used to route a question to whoever is
#: plausibly able to answer it, and to tell an answering agent what it is expected
#: to know. Curated rather than derived: an engine's *tasks* are not the same as
#: the questions it can settle, and guessing wrong sends a question somewhere it
#: will be answered badly rather than refused.
ANSWERABLE = {
    EngineName.DEVELOPMENT: (
        "what a product actually does and says, what it was built to achieve, "
        "what is deployed right now, what its acceptance criteria are, and what "
        "is known to be broken or accepted"
    ),
    EngineName.MARKETING: (
        "who is being contacted about a product, what has been sent, how people "
        "responded, and what the lead pipeline looks like"
    ),
    EngineName.COMPLIANCE: (
        "whether something is permitted -- data handling, outreach lawfulness, "
        "what an approval did or did not cover"
    ),
    EngineName.FINANCE: "what a product has earned or cost, and pricing decisions",
    EngineName.DISCOVERY: (
        "where an idea came from, what evidence supports it, and what was rejected"
    ),
    EngineName.DESIGN: (
        "how a product is supposed to look, what its design guidelines say, what a "
        "review of the live page found, and what the house style requires"
    ),
}

#: Recognises a question in an agent's own words. Agents are told to state one
#: plainly when blocked; this is tolerant because the alternative -- demanding a
#: rigid syntax -- is the thing that made the reporting tool fail. A blocked report
#: with no recognisable question still becomes a question, using the whole detail.
_QUESTION_RE = re.compile(r"QUESTION\s*(?:FOR\s+(?P<who>[A-Z_]+))?\s*:\s*(?P<text>.+)", re.I)


def parse_blocked_report(detail: str) -> tuple[str, EngineName | None, bool]:
    """Pull the question, and any named recipient, out of a blocked report.

    Returns ``(question, recipient, explicit)``. ``explicit`` is whether the agent
    actually marked a question with ``QUESTION:`` as its contract requires, or whether
    this is the generous fallback reading the whole report as one. The caller needs
    that distinction: a fallback reading is a guess, and a guess should not be spent
    on a person's attention without something else corroborating it.

    The recipient is ``None`` when the agent did not name one or named something
    unrecognised -- the caller then routes it.
    """
    text = (detail or "").strip()
    if not text:
        return "", None, False

    match = _QUESTION_RE.search(text)
    if match is None:
        # No marker. The whole report used to become the question, on the reasoning
        # that an agent explaining its problem in prose has still asked something.
        # That reasoning was wrong in one direction and it showed live: a repair agent
        # reported BLOCKED for a *failing test*, and the escalation that reached Jesse
        # read "development cannot proceed without knowing: `python -m pytest -q`
        # (exit code 1)". Not a question. A person's attention was spent on it and the
        # work parked for six hours waiting for an answer to nothing.
        #
        # "I failed" and "I need to know something" are different states. The result
        # contract already has a word for the first -- NOT_FIXED -- and an agent that
        # says BLOCKED without asking anything has used the wrong one. Treating that
        # as a question does not recover the mistake; it launders it into a request.
        #
        # The fix is not a stricter parser. Demanding a keyword is what made the old
        # reporting tool go permanently uncalled, and a phrasebook of interrogative
        # openings would reject "I cannot tell whether this page is meant to mention
        # the enterprise tier" -- a real question with no question mark in it.
        #
        # So this stays generous and the *caller* decides, using information it
        # already has: see `raise_blocked_question`. What went wrong in run 4 was not
        # this parse alone, it was two uncertainties compounding into a confident
        # action -- no marker found, no department matched, and a person escalated
        # anyway.
        return text, None, False

    question = match.group("text").strip()
    named = (match.group("who") or "").strip().lower()
    for engine in ANSWERABLE:
        if named == engine.value:
            return question, engine, True
    return question, None, True


def _matching_engines(question: str, asked_by: EngineName) -> list[EngineName]:
    """Departments whose subject matter appears in this text.

    Separate from `route` because the two answer different questions and the
    difference matters. `route` asks *who should answer*, and deliberately returns a
    person when nothing matches **or** when several do -- both mean "not confidently
    routable". But those are opposite evidence about whether the text is a question
    at all: zero matches means it mentions nothing anyone here owns, while two means
    it is squarely about this company's work and merely ambiguous about whose desk.
    Collapsing them cost a real question its delivery -- "are we allowed to email
    these prospects" matches Marketing on `prospects` and Compliance on `allowed to`.
    """
    lowered = question.lower()
    signals: dict[EngineName, tuple[str, ...]] = {
        EngineName.MARKETING: (
            "lead",
            "outreach",
            "campaign",
            "email sent",
            "prospect",
            "audience",
        ),
        EngineName.COMPLIANCE: ("gdpr", "consent", "lawful", "permitted", "privacy", "allowed to"),
        EngineName.FINANCE: ("price", "pricing", "revenue", "cost", "charge", "refund"),
        EngineName.DISCOVERY: ("idea come from", "evidence", "source of the idea", "why this idea"),
        EngineName.DEVELOPMENT: ("deployed", "the page", "requirement", "criteria", "workspace"),
        EngineName.DESIGN: ("design guideline", "house style", "look like", "visual", "branding"),
    }
    return [
        engine
        for engine, keywords in signals.items()
        # A department cannot answer its own question -- if it could, it would not
        # be blocked.
        if engine is not asked_by and any(word in lowered for word in keywords)
    ]


def route(question: str, asked_by: EngineName) -> str:
    """Who should answer this. Falls back to a person rather than guessing.

    Deliberately keyword-based and deliberately conservative. A misrouted question is
    worse than an unrouted one: it gets a confident answer from a department that does
    not actually know, and the asker cannot tell the difference. When nothing matches,
    a person sees it -- which is also the honest state of the organisation today,
    where most departments have no answering role at all.
    """
    matched = _matching_engines(question, asked_by)
    if len(matched) == 1:
        return matched[0].value
    # Nothing matched, or more than one did. Both mean the same thing *for routing*:
    # this cannot be sent confidently, so a person sees it. They mean opposite things
    # for "is this even a question", which is why `_matching_engines` is separate.
    #
    # Ambiguity is the case worth being careful about. "Are we allowed to email these
    # prospects?" reads as Marketing on `prospects` and Compliance on `allowed to`,
    # and picking by dictionary order would send a lawfulness question to the
    # department with the least standing to answer it -- which would come back
    # confident and wrong, indistinguishable to the asker from a real answer.
    return repository.HUMAN


#: The paragraph every decision-maker in this company gets, verbatim.
#:
#: Shared rather than written per department, because the failure it prevents is not
#: department-specific: a manager that cannot establish a fact will otherwise invent
#: it or decline to act, and both are worse than asking. Any prompt asking a model to
#: *decide* something should include this; a prompt asking one to *do* something
#: already has the `RESULT: BLOCKED` contract instead.
ASKING_GUIDANCE = """\
Not knowing is not a reason to do nothing -- it is a reason to find out. If what \
stops you deciding is a fact somebody else in this company holds, put that question \
in `needs_to_know` rather than guessing, and rather than declining because you are \
unsure. Ask for a fact, not for permission, and ask the smallest question that would \
actually change your decision -- a broad question gets a broad answer that settles \
nothing. Your work parks and resumes with the answer, so asking costs you a delay \
and nothing else.

**Explaining your uncertainty is not asking.** Nobody reads your assessment. If the \
question is written there and `needs_to_know` is empty, it reaches no one, nobody \
answers it, and the work stops for a fact that was never requested -- which looks \
from outside exactly like a department that decided there was nothing to do. A \
question only travels if it is in `needs_to_know`.

The one thing you must never do is invent the answer and act on it.\
"""


async def ask(
    session: AsyncSession,
    *,
    asked_by: EngineName,
    question: str,
    context: str = "",
    ask_of: str = "",
    blocking_task_id: str | None = None,
    asked_by_task_id: str | None = None,
    product_id: str | None = None,
) -> Question | None:
    """Ask another part of the company something, and put it in front of them.

    The single entry point, whatever is doing the asking. An agent mid-run reaches it
    through `raise_blocked_question`, which parses its BLOCKED report first; a manager
    deciding what work is worth doing reaches it through `raise_decision_questions`,
    because a structured decision has no result contract to report through. Both
    produce the same record, the same routing and the same delivery -- which is the
    reason this lives here rather than in either caller.

    ``ask_of`` names a department when the caller knows one. Anything unrecognised is
    routed rather than trusted, and an unroutable question reaches a person rather
    than going nowhere.
    """
    text = question.strip()
    if not text:
        # Nothing to ask is not a question. Inventing one on the caller's behalf
        # would put a fabricated request in front of whoever receives it.
        log.info("comms.empty_question", asked_by=asked_by.value)
        return None

    named = ask_of.strip().lower()
    asked_of = named if named in {engine.value for engine in ANSWERABLE} else route(text, asked_by)

    row = await repository.ask_question(
        session,
        asked_by_engine=asked_by.value,
        asked_of=asked_of,
        question=text,
        context=context,
        asked_by_task_id=asked_by_task_id,
        blocking_task_id=blocking_task_id,
        product_id=product_id,
    )
    await deliver(session, row, asked_by=asked_by)
    return row


async def park_until_answered(session: AsyncSession, task_id: str, *, reason: str) -> None:
    """Hold work that is waiting on somebody else, without counting it as a failure.

    Deferred rather than failed, and the distinction is load-bearing: waiting on a
    colleague is not an attempt at anything, so counting it as one would spend a
    product's repair budget on other people's response times.
    """
    await repository.defer_task(
        session,
        task_id,
        not_before=datetime.now(UTC) + timedelta(hours=BLOCKED_RETRY_HOURS),
        reason=reason,
    )


async def answers_for(session: AsyncSession, task_id: str) -> str:
    """Answers this task was waiting on, rendered for its prompt. ``""`` if none.

    Every resuming caller needs this and none of them should implement it: work
    released by an answer and then re-run *without* the answer blocks again on exactly
    the same question, which is a loop rather than a conversation.
    """
    return render_answers(await repository.answered_questions_for_task(session, task_id))


async def raise_decision_questions(
    session: AsyncSession,
    *,
    asked_by: EngineName,
    decision: ManagerDecision,
    task_id: str,
    product_id: str | None = None,
) -> int:
    """Ask everything a manager said it needed to know, and park its tick. Returns how many.

    The one call a department manager makes, which is the point: a Marketing or
    Finance manager gets this behaviour by inheriting `ManagerDecision` and calling
    this, with no comms code of its own to get subtly wrong.

    The *tick* is what parks, not the work it would have commissioned. That is
    deliberate -- the manager has not decided yet, and a decision made without the
    answer is exactly the guess it declined to make. Whatever it was sure enough to
    commission alongside its questions still goes ahead; being unsure about one thing
    is not being unsure about everything.
    """
    asked = 0
    for item in decision.needs_to_know:
        row = await ask(
            session,
            asked_by=asked_by,
            question=item.question,
            context=decision.assessment[:1000],
            ask_of=item.ask_of,
            asked_by_task_id=task_id,
            blocking_task_id=task_id,
            product_id=product_id,
        )
        if row is not None:
            asked += 1

    if asked:
        await park_until_answered(
            session, task_id, reason=f"{asked_by.value} manager waiting on {asked} question(s)"
        )
    return asked


async def raise_blocked_question(
    session: AsyncSession,
    *,
    asked_by: EngineName,
    detail: str,
    blocking_task_id: str | None,
    asked_by_task_id: str | None = None,
    product_id: str | None = None,
    context: str = "",
) -> Question | None:
    """Turn a blocked agent's report into a routed question, and park its work.

    ``None`` when the report carried nothing to ask about -- an agent that reported
    BLOCKED with no explanation has not asked a question, and inventing one on its
    behalf would put a fabricated request in front of whoever receives it.
    """
    question_text, named, explicit = parse_blocked_report(detail)
    if not question_text:
        log.info("comms.blocked_without_question", asked_by=asked_by.value)
        return None

    # **Two uncertainties must not compound into a confident action.**
    #
    # The agent did not mark a question, so reading one out of its report is a guess.
    # Routing then found no department that plausibly holds the answer, so naming a
    # recipient is a second guess. Neither alone is fatal -- a prose question about an
    # identifiable subject routes fine, and a marked question nobody can answer is
    # exactly what a person is for. Both together mean we have no evidence this is a
    # question at all.
    #
    # That combination is what reached Jesse in run 4: a repair agent said BLOCKED
    # about a failing test, and the escalation read "development cannot proceed
    # without knowing: `python -m pytest -q` (exit code 1)". Six hours of parked work
    # and a person's attention, spent on a command trace.
    #
    # Returning None here sends it back down the ordinary repair path, which is where
    # a failed repair belongs. It also removes the incentive the contract creates:
    # BLOCKED is advertised to agents as not counting against the repair budget, so
    # for an agent that simply could not fix something it is the cheaper word. Now
    # using it without asking anything buys nothing.
    if not explicit and named is None:
        if not _matching_engines(question_text, asked_by):
            log.info(
                "comms.blocked_without_a_request",
                asked_by=asked_by.value,
                detail=question_text[:200],
            )
            return None

    return await ask(
        session,
        asked_by=asked_by,
        question=question_text,
        context=context,
        ask_of=named.value if named is not None else "",
        blocking_task_id=blocking_task_id,
        asked_by_task_id=asked_by_task_id,
        product_id=product_id,
    )


async def deliver(session: AsyncSession, question: Question, *, asked_by: EngineName) -> None:
    """Put a recorded question in front of whoever has to answer it.

    Delivery is separate from recording because the two recipients are not alike: a
    department gets a task it will pick up like any other work, a person gets an
    interrupt. Every asker funnels through here via `ask`, so neither kind of
    recipient is something a caller has to know about -- which is what stops the next
    department inventing its own half of this and getting the person case wrong.
    """
    if question.asked_of == repository.HUMAN:
        # A question nobody in the organisation can answer is exactly what an
        # interrupt is for, and reaching a person beats silence even when no
        # department could have helped.
        await interrupts.request(
            session,
            reason=InterruptReason.DATA_INTEGRITY,
            subject=f"{asked_by.value} is blocked and needs an answer",
            detail=(
                f"{asked_by.value} cannot proceed without knowing:\n\n"
                f"{question.question}\n\n"
                + (f"What it already knows:\n{question.context}\n\n" if question.context else "")
                + "The work is parked, not failed. Answering releases it."
            ),
            headline="❓ A department is blocked on a question",
            reply_hint="Reply with the answer and the blocked work resumes.",
            product_id=question.product_id,
        )
        return

    await repository.enqueue_task(
        session,
        kind=TaskKind.ANSWER_QUESTION,
        engine=EngineName(question.asked_of),
        idempotency_key=f"answer:{question.id}",
        payload={"question_id": question.id},
        product_id=question.product_id,
        # Above ordinary build work: somebody else is parked waiting on this, so
        # answering late costs more than the answer is worth.
        priority=110,
    )


def render_answers(answers: list[Question]) -> str:
    """Answers a resuming task was waiting on, for its brief.

    Without this the channel is pointless -- work would be released and then re-run
    with exactly the information that blocked it the first time.
    """
    if not answers:
        return ""
    lines = ["You asked, and were answered. Use this rather than guessing again:"]
    for row in answers:
        lines += [f"\nQ ({row.asked_of}): {row.question}", f"A: {row.answer or '(no answer)'}"]
    return "\n".join(lines)
