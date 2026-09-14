"""The Design manager: a department that decides, and asks when it cannot.

These pin the two things that were actually wrong when the manager first ran live,
both of which would pass a reading of the code:

- It had no way to ask, so it correctly commissioned nothing and looked idle.
- Once it could ask, the tick returned on `if not decision.actions` *before*
  raising the questions -- and "I cannot establish whether this matters, so I am
  asking" is the likeliest shape of a tick with questions, not a rare one.

The second is the more dangerous kind of bug: nothing errors, a question is simply
never asked, and the log line says "nothing worth doing" in a tone that reads as a
judgement the manager made.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from factory.engines.design import manager
from factory.settings import EngineName
from factory.state import repository
from factory.state.schemas import Ask, DesignAction, DesignDecision, TaskKind


@pytest.fixture
def channel(monkeypatch) -> dict:
    """Records what the manager did, without a database."""
    seen: dict = {"asked": [], "enqueued": [], "parked": None, "events": []}

    async def fake_domain(session):
        return repository.DesignDomain(
            without_guidelines=[
                repository.DesignSubject(
                    id="p-1", name="Shardline", status="planned", deployed=True
                )
            ],
            unreviewed=[],
            open_findings=[],
            open_questions=0,
        )

    async def fake_ask_question(session, **kwargs):
        from factory.state.orm import Question

        seen["asked"].append(kwargs)
        return Question(
            id=f"q-{len(seen['asked'])}",
            asked_by_engine=kwargs["asked_by_engine"],
            asked_of=kwargs["asked_of"],
            question=kwargs["question"],
            context=kwargs.get("context", ""),
        )

    async def fake_enqueue(session, **kwargs):
        seen["enqueued"].append(kwargs)
        return f"t-{len(seen['enqueued'])}"

    async def fake_defer(session, task_id, **kwargs):
        seen["parked"] = (task_id, kwargs.get("reason"))

    async def fake_event(session, **kwargs):
        seen["events"].append(kwargs)

    async def no_answers(session, task_id):
        return []

    monkeypatch.setattr(repository, "design_domain", fake_domain)
    monkeypatch.setattr(repository, "ask_question", fake_ask_question)
    monkeypatch.setattr(repository, "enqueue_task", fake_enqueue)
    monkeypatch.setattr(repository, "defer_task", fake_defer)
    monkeypatch.setattr(repository, "record_task_event", fake_event)
    monkeypatch.setattr(repository, "answered_questions_for_task", no_answers)
    return seen


def _ctx(decision: DesignDecision) -> SimpleNamespace:
    async def parse(*args, **kwargs):
        return decision

    return SimpleNamespace(llm=SimpleNamespace(parse=parse))


async def test_a_manager_with_only_a_question_still_asks_it(channel) -> None:
    """The ordering bug, and the reason it matters.

    A tick that decides nothing *because* it could not establish something is the
    normal shape of asking, not an edge case. Returning early on "no actions" reads
    as correct and silently drops the question -- the department then logs "nothing
    worth doing" and waits for a fact nobody was ever asked for.
    """
    decision = DesignDecision(
        assessment="One product has no guidelines, but I cannot tell if it is still live.",
        needs_to_know=[Ask(question="Is Shardline still being sold?", ask_of="finance")],
    )

    result = await manager.run_manager_tick(
        _ctx(decision), object(), SimpleNamespace(id="tick-1", payload={})
    )

    assert len(channel["asked"]) == 1
    assert channel["asked"][0]["asked_of"] == "finance"
    assert channel["parked"] is not None and channel["parked"][0] == "tick-1"
    assert "asked 1 question" in result
    assert "nothing worth doing" not in result


async def test_being_unsure_about_one_thing_is_not_being_unsure_about_everything(
    channel,
) -> None:
    """A question must not cancel the work the manager was already sure about.

    Otherwise asking carries a cost the manager can feel -- it loses this tick's
    commissioned work -- and a manager that is penalised for asking learns to guess,
    which is the behaviour the whole channel exists to prevent.
    """
    decision = DesignDecision(
        assessment="Shardline needs guidelines. Separately, I cannot tell who the buyer is.",
        needs_to_know=[Ask(question="Who is the buyer for Shardline?", ask_of="marketing")],
        actions=[
            DesignAction(
                kind="write_guidelines",
                product_id="p-1",
                why="It is live with no guidelines written for it at all.",
            )
        ],
    )

    await manager.run_manager_tick(
        _ctx(decision), object(), SimpleNamespace(id="tick-2", payload={})
    )

    assert len(channel["asked"]) == 1
    # Two enqueues, and both are meant: delivering the question to Marketing, and
    # the guidelines work Design was sure enough to commission anyway.
    commissioned = [
        row for row in channel["enqueued"] if row["kind"] is not TaskKind.ANSWER_QUESTION
    ]
    assert len(commissioned) == 1
    assert commissioned[0]["kind"] is TaskKind.WRITE_DESIGN_GUIDELINES
    assert commissioned[0]["engine"] is EngineName.DESIGN
    assert channel["parked"] is not None


async def test_a_manager_that_decided_nothing_and_asked_nothing_is_not_parked(channel) -> None:
    """Nothing worth doing is a correct answer, and must not stall the department.

    A department that finds work every day is manufacturing it. Parking a tick that
    simply had nothing to do would take the department off the board for six hours
    for being right.
    """
    decision = DesignDecision(
        assessment="Every live product has guidelines and no findings are open. Nothing to do."
    )

    result = await manager.run_manager_tick(
        _ctx(decision), object(), SimpleNamespace(id="tick-3", payload={})
    )

    assert channel["asked"] == []
    assert channel["parked"] is None
    assert "nothing worth doing" in result


async def test_an_answer_is_put_back_in_front_of_the_manager(channel, monkeypatch) -> None:
    """Without this the round trip buys nothing.

    The tick resumes because somebody answered; if the answer is not in the prompt
    it asks the same question again, and the department loops at six-hour intervals
    instead of holding a conversation.
    """
    from factory.state.orm import Question

    async def one_answer(session, task_id):
        return [
            Question(
                id="q-old",
                asked_by_engine="design",
                asked_of="finance",
                question="Is Shardline still being sold?",
                context="",
                status="answered",
                answer="[established] Yes, two paying customers.",
            )
        ]

    monkeypatch.setattr(repository, "answered_questions_for_task", one_answer)

    prompts: list[str] = []

    async def parse(*args, **kwargs):
        prompts.append(kwargs["prompt"])
        return DesignDecision(assessment="It is still sold, so the guidelines are worth writing.")

    await manager.run_manager_tick(
        SimpleNamespace(llm=SimpleNamespace(parse=parse)),
        object(),
        SimpleNamespace(id="tick-4", payload={}),
    )

    assert "two paying customers" in prompts[0]
