"""Asking is a state of the work; answering is work. Neither is a tool.

Jesse's question decided the design: *"should it be a tool? Shouldn't comms be
inherent to the system?"* These tests pin the properties that follow from the
answer, because they are easy to lose to a well-meaning refactor that adds an
`ask_question` tool back.

The evidence that a tool is the wrong shape is already in this codebase.
`report_product_issue` shipped in AUDITOR's own registry and *"both live runs spent
their whole step budget investigating and never chose to call it, so a genuine
review was performed and then discarded."* An agent must remember to call a tool,
competing for steps against the work -- and the agent most in need of asking is the
confused one, least likely to spend a step on it.
"""

from __future__ import annotations

from pydantic import Field

from factory import comms
from factory.settings import EngineName
from factory.state import repository


def test_a_question_is_recognised_in_the_agents_own_words() -> None:
    """The marker is a convenience, not a requirement.

    Demanding rigid syntax is the failure mode that made the reporting tool
    useless. An agent that explained its problem in prose has still asked
    something, and discarding that for missing a keyword is how a channel goes
    unused.
    """
    marked, who = comms.parse_blocked_report(
        "Ran the suite, 4 passed.\nQUESTION: is the $199 price final or a placeholder?"
    )
    assert marked == "is the $199 price final or a placeholder?"
    assert who is None

    prose, _ = comms.parse_blocked_report(
        "I cannot tell whether this page is meant to mention the enterprise tier."
    )
    assert "enterprise tier" in prose


def test_an_agent_can_name_the_department_it_needs() -> None:
    question, who = comms.parse_blocked_report(
        "QUESTION FOR MARKETING: which audience segment is this page being sent to?"
    )
    assert who is EngineName.MARKETING
    assert question.startswith("which audience segment")


def test_an_unrecognised_department_is_routed_rather_than_dropped() -> None:
    """A question addressed to something that does not exist still gets asked."""
    question, who = comms.parse_blocked_report("QUESTION FOR LEGAL: is this claim allowed?")
    assert who is None
    assert question == "is this claim allowed?"


def test_blocked_with_nothing_to_ask_is_not_a_question() -> None:
    """An agent that reported BLOCKED and explained nothing has not asked anything.

    Inventing a question on its behalf would put a fabricated request in front of
    whoever receives it, and they would answer it.
    """
    assert comms.parse_blocked_report("") == ("", None)
    assert comms.parse_blocked_report("   ") == ("", None)


def test_routing_falls_back_to_a_person_rather_than_guessing() -> None:
    """A misrouted question is worse than an unrouted one.

    It gets a confident answer from a department that does not actually know, and
    the asker cannot tell the difference. Reaching a person is also the honest
    state of this organisation, where most departments have no answering role.
    """
    assert (
        comms.route("is the favicon meant to be the full logo?", EngineName.DEVELOPMENT)
        == repository.HUMAN
    )


def test_routing_recognises_what_a_department_actually_holds() -> None:
    assert (
        comms.route("which audience are we sending outreach to?", EngineName.DEVELOPMENT)
        == EngineName.MARKETING.value
    )
    assert (
        comms.route("is storing this field permitted under gdpr?", EngineName.DEVELOPMENT)
        == EngineName.COMPLIANCE.value
    )
    assert comms.route("is the price final?", EngineName.DEVELOPMENT) == EngineName.FINANCE.value


def test_a_department_is_never_routed_its_own_question() -> None:
    """If it could answer, it would not be blocked."""
    assert (
        comms.route("what does the deployed page say?", EngineName.DEVELOPMENT) == repository.HUMAN
    )


def test_an_answer_reaches_the_work_that_was_waiting() -> None:
    """Without this the channel is decorative.

    Released work would re-run with exactly the information that blocked it, and
    block again on the same thing.
    """
    from factory.state.orm import Question

    answered = Question(
        id="q1",
        asked_by_engine="development",
        asked_of="marketing",
        question="which segment is this for?",
        context="",
        status="answered",
        answer="[established] Mobile engineering leads at agencies.",
    )

    rendered = comms.render_answers([answered])

    assert "which segment is this for?" in rendered
    assert "Mobile engineering leads" in rendered
    assert comms.render_answers([]) == ""


def test_an_ambiguous_question_goes_to_a_person_rather_than_the_first_match() -> None:
    """The case that matters most, and the one a keyword router gets wrong quietly.

    "Are we allowed to email these prospects?" reads as Marketing on `prospects`
    and Compliance on `allowed to`. Picking by dictionary order would send a
    lawfulness question to the department with the least standing to answer it --
    and it would come back confident and wrong, indistinguishable to the asker from
    a real answer.
    """
    assert (
        comms.route("are we allowed to email these prospects?", EngineName.DEVELOPMENT)
        == repository.HUMAN
    )


def test_asking_is_a_state_of_the_decision_not_an_action_it_elects() -> None:
    """The thesis of this module, pinned where a refactor would break it.

    `report_product_issue` proved an elected action loses: both live runs spent
    their whole step budget investigating and never chose to call it. The same
    mistake is easy to make again in a decision schema -- an `ask` entry in an
    action list competes with real work for one of a handful of slots, and the
    manager most in need of asking is the one with the least to spare.

    So `needs_to_know` is a field on every manager's decision, filled in while it
    describes its situation, and no department's action list contains "ask".
    """
    from factory.state.schemas import DesignAction, ManagerDecision

    assert "needs_to_know" in ManagerDecision.model_fields

    for action in _manager_action_schemas():
        kinds = action.model_fields["kind"].annotation
        assert "ask" not in getattr(kinds, "__args__", ()), f"{action.__name__} elects to ask"

    assert DesignAction in _manager_action_schemas()


def _manager_action_schemas() -> list[type]:
    """Every action schema a department manager commissions through."""
    from factory.state.schemas import DesignAction

    return [DesignAction]


def test_every_department_that_can_be_asked_can_answer() -> None:
    """A question routed to a department whose runner drops it is worse than none.

    It is recorded, delivered, and waited on forever -- the asker's work parks and
    its answer never comes, which looks from the outside exactly like a hang. The
    registry and the runners have to agree, and nothing but this makes them.
    """
    import importlib

    from factory.state.schemas import TaskKind

    for engine in comms.ANSWERABLE:
        graph = importlib.import_module(f"factory.engines.{engine.value}.graph")
        source = graph.__file__ or ""
        assert source, f"{engine.value} has no runner"
        with open(source, encoding="utf-8") as handle:
            assert TaskKind.ANSWER_QUESTION.name in handle.read(), (
                f"{engine.value} can be asked questions but its runner never answers one"
            )


async def test_a_manager_asks_and_parks_without_writing_any_comms_code(monkeypatch) -> None:
    """The generality claim, tested with a department that does not exist.

    The point of `raise_decision_questions` is that the next manager -- Marketing,
    Finance, whoever -- inherits this by subclassing `ManagerDecision` and making
    one call. If this test needs anything department-specific to pass, the channel
    is a Design feature wearing a general name.
    """
    from factory.state.schemas import Ask, ManagerDecision

    class SupplyDecision(ManagerDecision):
        """A department invented for this test. No comms code of its own."""

        actions: list[str] = Field(default_factory=list)

    recorded: dict[str, object] = {}

    async def fake_ask_question(session, **kwargs):
        from factory.state.orm import Question

        recorded["asked"] = kwargs
        return Question(
            id="q-1",
            product_id=None,
            **{
                k: v
                for k, v in kwargs.items()
                if k in {"asked_by_engine", "asked_of", "question", "context"}
            },
        )

    async def fake_enqueue(session, **kwargs):
        recorded["delivered_to"] = kwargs.get("engine")
        return "t-1"

    async def fake_defer(session, task_id, **kwargs):
        recorded["parked"] = (task_id, kwargs.get("reason"))

    monkeypatch.setattr(repository, "ask_question", fake_ask_question)
    monkeypatch.setattr(repository, "enqueue_task", fake_enqueue)
    monkeypatch.setattr(repository, "defer_task", fake_defer)

    decision = SupplyDecision(
        assessment="Cannot tell whether this product is still being sold.",
        needs_to_know=[Ask(question="Is this product still being sold?", ask_of="finance")],
    )

    asked = await comms.raise_decision_questions(
        object(), asked_by=EngineName.DESIGN, decision=decision, task_id="tick-1"
    )

    assert asked == 1
    assert recorded["asked"]["asked_of"] == "finance"
    # The assessment travels with the question: an answerer that cannot see why it
    # was asked answers the words rather than the need.
    assert "still being sold" in str(recorded["asked"]["context"])
    assert recorded["delivered_to"] == EngineName.FINANCE
    # The *tick* parks, not the work it would have commissioned -- the manager has
    # not decided yet, and deciding without the answer is the guess it declined.
    assert recorded["parked"][0] == "tick-1"


async def test_a_manager_that_asks_nothing_is_never_parked(monkeypatch) -> None:
    """Parking a tick that asked nothing would stall a department for six hours."""

    async def explode(*args, **kwargs):
        raise AssertionError("a manager with no questions must not touch the channel")

    monkeypatch.setattr(repository, "ask_question", explode)
    monkeypatch.setattr(repository, "defer_task", explode)

    from factory.state.schemas import ManagerDecision

    decision = ManagerDecision(assessment="Everything here looks fine, nothing to do.")
    asked = await comms.raise_decision_questions(
        object(), asked_by=EngineName.DESIGN, decision=decision, task_id="tick-2"
    )
    assert asked == 0
