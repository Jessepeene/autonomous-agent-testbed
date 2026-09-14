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
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Question
from factory.state.schemas import InterruptReason, TaskKind

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
}

#: Recognises a question in an agent's own words. Agents are told to state one
#: plainly when blocked; this is tolerant because the alternative -- demanding a
#: rigid syntax -- is the thing that made the reporting tool fail. A blocked report
#: with no recognisable question still becomes a question, using the whole detail.
_QUESTION_RE = re.compile(r"QUESTION\s*(?:FOR\s+(?P<who>[A-Z_]+))?\s*:\s*(?P<text>.+)", re.I)


def parse_blocked_report(detail: str) -> tuple[str, EngineName | None]:
    """Pull the question, and any named recipient, out of a blocked report.

    Returns ``(question, recipient)``. The recipient is ``None`` when the agent did
    not name one or named something unrecognised -- the caller then routes it, and
    routing failure means a person, never silence.
    """
    text = (detail or "").strip()
    if not text:
        return "", None

    match = _QUESTION_RE.search(text)
    if match is None:
        # No marker: the whole report is the question. An agent that explained its
        # problem in prose has still asked something, and discarding that for
        # failing to use a keyword is how a channel goes unused.
        return text, None

    question = match.group("text").strip()
    named = (match.group("who") or "").strip().lower()
    for engine in ANSWERABLE:
        if named == engine.value:
            return question, engine
    return question, None


def route(question: str, asked_by: EngineName) -> str:
    """Who should answer this. Falls back to a person rather than guessing.

    Deliberately keyword-based and deliberately conservative. A misrouted question
    is worse than an unrouted one: it gets a confident answer from a department
    that does not actually know, and the asker cannot tell the difference. When
    nothing matches, a person sees it -- which is also the honest state of the
    organisation today, where most departments have no answering role at all.
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
    }
    matched = [
        engine
        for engine, keywords in signals.items()
        # A department cannot answer its own question -- if it could, it would not
        # be blocked.
        if engine is not asked_by and any(word in lowered for word in keywords)
    ]
    if len(matched) == 1:
        return matched[0].value
    # Nothing matched, or more than one did. Both mean the same thing: this cannot
    # be routed confidently, so a person sees it.
    #
    # Ambiguity is the case worth being careful about. "Are we allowed to email
    # these prospects?" reads as Marketing on `prospects` and Compliance on
    # `allowed to`, and picking by dictionary order would send a lawfulness
    # question to the department with the least standing to answer it -- which
    # would come back confident and wrong, indistinguishable to the asker from a
    # real answer.
    return repository.HUMAN


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
    question_text, named = parse_blocked_report(detail)
    if not question_text:
        log.info("comms.blocked_without_question", asked_by=asked_by.value)
        return None

    asked_of = named.value if named is not None else route(question_text, asked_by)
    row = await repository.ask_question(
        session,
        asked_by_engine=asked_by.value,
        asked_of=asked_of,
        question=question_text,
        context=context,
        asked_by_task_id=asked_by_task_id,
        blocking_task_id=blocking_task_id,
        product_id=product_id,
    )

    if asked_of == repository.HUMAN:
        # The existing path, unchanged. A question nobody in the organisation can
        # answer is exactly what an interrupt is for, and reaching a person beats a
        # silent retry even when no department could have helped.
        await interrupts.request(
            session,
            reason=InterruptReason.DATA_INTEGRITY,
            subject=f"{asked_by.value} is blocked and needs an answer",
            detail=(
                f"{asked_by.value} cannot proceed without knowing:\n\n{question_text}\n\n"
                + (f"What it already knows:\n{context}\n\n" if context else "")
                + "The work is parked, not failed. Answering releases it."
            ),
            headline="❓ A department is blocked on a question",
            reply_hint="Reply with the answer and the blocked work resumes.",
            product_id=product_id,
        )
    else:
        await repository.enqueue_task(
            session,
            kind=TaskKind.ANSWER_QUESTION,
            engine=EngineName(asked_of),
            idempotency_key=f"answer:{row.id}",
            payload={"question_id": row.id},
            product_id=product_id,
            # Above ordinary build work: somebody else is parked waiting on this,
            # so answering late costs more than the answer is worth.
            priority=110,
        )
    return row


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
