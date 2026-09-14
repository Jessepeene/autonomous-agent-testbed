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
    assert (
        comms.route("is the price final?", EngineName.DEVELOPMENT) == EngineName.FINANCE.value
    )


def test_a_department_is_never_routed_its_own_question() -> None:
    """If it could answer, it would not be blocked."""
    assert (
        comms.route("what does the deployed page say?", EngineName.DEVELOPMENT)
        == repository.HUMAN
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
