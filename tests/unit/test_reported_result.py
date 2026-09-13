"""The blocked signal — a channel that existed as a convention and was never read.

`agents/roles.py::_REPAIR_RULES` has always instructed every repair agent to end
with `RESULT: FIXED | NOT_FIXED | BLOCKED` plus the command it ran and its exit
code. `_SHARED_RULES` tells every other role to "say so plainly and state exactly
what is missing" when it cannot proceed. **No code parsed either.**

The cost of that was concrete: a repair agent spent three separate live runs
discovering its own toolchain had been deleted before it started, said so, and was
recorded each time as having simply failed — indistinguishable from an agent that
tried and could not fix the bug.

These tests pin the parser, not a policy. What the factory *does* about a blocker
is deliberately not decided yet (see ARCHITECTURE_ROADMAP.md): record first, then
choose the rule from real blockers rather than inventing one at a desk.
"""

from __future__ import annotations

import pytest

from factory.agents.runtime import AgentResult, parse_reported_result


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ("RESULT: FIXED", "FIXED"),
        ("RESULT: NOT_FIXED", "NOT_FIXED"),
        ("RESULT: BLOCKED", "BLOCKED"),
        ("result: blocked", "BLOCKED"),
        ("RESULT:BLOCKED", "BLOCKED"),
    ],
)
def test_the_closed_vocabulary_is_recognised(answer: str, verdict: str) -> None:
    assert parse_reported_result(answer)[0] == verdict


def test_the_detail_after_the_verdict_is_captured() -> None:
    """The rule asks for the command and its exit code alongside the verdict, and
    that trailing half is the part that says *what* is wrong."""
    verdict, detail = parse_reported_result(
        "I could not run the suite.\nRESULT: BLOCKED pytest -q, exit 127: pytest not installed"
    )
    assert verdict == "BLOCKED"
    assert "127" in detail
    assert "not installed" in detail


def test_a_report_buried_mid_message_is_still_found() -> None:
    """The rule says "end your final message with one line, exactly". Models
    approximate that more often than they obey it, and a strict last-line match
    would discard most real reports — which is the exact failure this channel
    exists to end."""
    verdict, _ = parse_reported_result(
        "RESULT: BLOCKED — no dependencies installed.\n\nHappy to retry once fixed."
    )
    assert verdict == "BLOCKED"


def test_the_final_report_wins_over_a_narrated_plan() -> None:
    """An agent that says what it intends to report and then reports should be
    taken at its last word, not its first."""
    verdict, _ = parse_reported_result(
        "I will end with RESULT: FIXED once the tests pass.\n"
        "The tests did not pass.\n"
        "RESULT: NOT_FIXED pytest -q, exit 1"
    )
    assert verdict == "NOT_FIXED"


@pytest.mark.parametrize(
    "answer",
    ["", "The repair went fine.", "RESULT: MAYBE", "RESULTS: FIXED", "no sentinel here"],
)
def test_no_report_is_reported_as_no_report(answer: str) -> None:
    """Absence must be distinguishable from a verdict — an agent that said nothing
    has not claimed success."""
    assert parse_reported_result(answer) == ("", "")


def test_reported_blocked_is_independent_of_how_the_runtime_stopped() -> None:
    """`stopped_because` is the runtime's account of why it stopped the agent; this
    is the agent's account of why it could not proceed. They answer different
    questions and an agent can be blocked while completing normally."""
    completed_but_blocked = AgentResult(
        objective="fix it",
        answer="RESULT: BLOCKED pytest not installed",
        steps=4,
        stopped_because="completed",
        reported_result="BLOCKED",
    )
    assert completed_but_blocked.succeeded
    assert completed_but_blocked.reported_blocked

    silent = AgentResult(objective="fix it", answer="done", steps=4)
    assert silent.reported_blocked is False
