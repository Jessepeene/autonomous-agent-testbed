"""Answering a question from another department, as work rather than a tool call.

The other half of `factory/comms.py`. A question addressed to a department arrives
as an ordinary task in its queue and is picked up like anything else -- which is
the whole point of not making this a tool. Nobody has to notice it, no agent has to
remember to check for it, and the answer is durable and attributable without anyone
choosing to log it.

What an answer is allowed to be matters as much as that it happens. An answer that
sounds confident and is invented is worse than no answer, because the asker cannot
tell the difference and will proceed on it -- so the prompt below makes "I do not
know, and here is who would" a first-class answer rather than a failure.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.comms import ANSWERABLE
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import DepartmentAnswer
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

ANSWER_SYSTEM = """\
Another department is blocked and has asked you something. Answer it.

Somebody's work is parked waiting on this, so a vague answer costs as much as no \
answer -- they will either proceed on it or ask again.

Answer only from what you can actually establish. You have the product's own \
record: its acceptance criteria, its open issues, its history, what is deployed. \
Use it, and say which part of it you are relying on, so the asker can judge how \
much weight your answer carries.

**"I do not know" is a real answer and sometimes the right one.** If the record \
does not settle it, say so plainly and say who or what would -- a person, another \
department, a check nobody has run. An invented answer is worse than an absent \
one, because the asker cannot tell the difference and will build on it.

Be specific and be brief. The reader is an agent that will act on this, not a \
person skimming a report.\
"""


async def answer_question_task(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Answer one question, and release whatever was waiting on it."""
    question_id = str(task.payload.get("question_id", ""))
    question = await repository.get_question(session, question_id) if question_id else None
    if question is None:
        return f"answering: no question {question_id!r} to answer"
    if question.status != "open":
        # Already answered, or withdrawn because the asker moved on. Not an error:
        # the queue is at-least-once and a duplicate must not overwrite a real answer.
        return f"answering: question {question_id} is already {question.status}"

    engine = EngineName(question.asked_of)
    dossier = ""
    if question.product_id:
        from factory.handbook import product_dossier

        dossier = await product_dossier(session, question.product_id)

    prompt = (
        f"{question.asked_by_engine} asks:\n\n{question.question}\n\n"
        + (f"What they already know:\n{question.context}\n\n" if question.context else "")
        + f"You are {engine.value}. You are expected to be able to answer about: "
        + f"{ANSWERABLE.get(engine, 'your own department')}.\n\n"
        + (f"The record you hold on this product:\n\n{dossier}\n\n" if dossier else "")
        + "Answer the question."
    )

    try:
        reply = await ctx.llm.parse(
            DepartmentAnswer,
            task_type=TaskType.ANALYSIS,
            system=ANSWER_SYSTEM,
            prompt=prompt,
            engine=engine,
            task_id=task.id,
            max_tokens=8_000,
        )
    except Exception as exc:
        # A question that cannot be answered must not vanish. Leaving it open means
        # the asker's deferred work retries on its own schedule rather than waiting
        # on an answer that is never coming.
        log.warning("answering.failed", question_id=question_id, detail=str(exc)[:200])
        return f"answering: could not answer {question_id} -- {str(exc)[:150]}"

    # Rendered with its confidence attached, because the asker has to be able to
    # tell a settled answer from a guess -- that distinction is the whole reason the
    # reply is structured rather than prose.
    rendered = f"[{reply.confidence}] {reply.answer}"
    if reply.basis:
        rendered += f"\n\nBased on: {reply.basis}"
    if reply.confidence == "unknown" and reply.who_would_know:
        rendered += f"\n\nWho would know: {reply.who_would_know}"

    released = await repository.answer_question(
        session, question_id=question_id, answer=rendered, answered_by_task_id=task.id
    )
    log.info(
        "answering.answered",
        question_id=question_id,
        asked_by=question.asked_by_engine,
        confidence=reply.confidence,
        released_task=released or "none",
    )
    return (
        f"answering: answered {question.asked_by_engine}'s question"
        + (f", released task {released}" if released else "")
    )
