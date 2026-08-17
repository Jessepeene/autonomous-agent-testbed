"""The agent runtime — the loop that actually executes tools.

Before this loop existed, tools were declared, filtered, and rendered into a
provider payload, and then never invoked: a model could ask for a tool and nothing
would happen. These tests drive the loop with a `FakeProvider` standing in for the
model and assert the containment properties (allowlist, step ceiling, deadline,
unknown-tool handling) that make it safe to hand an LLM a tool belt.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from factory.agents.runtime import AgentRuntime, _compact_to_budget, _context_chars, _turn_chars
from factory.llm.client import LLMClient
from factory.llm.providers.base import (
    AgentTurn,
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    ToolCall,
    ToolResult,
    Turn,
    Usage,
)
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.settings import Settings, TaskType
from factory.tools.registry import ToolDescriptor, ToolRegistry


def _client(
    settings: Settings,
    *,
    gemini: FakeProvider | None = None,
    anthropic: FakeProvider | None = None,
) -> LLMClient:
    providers: dict[Provider, FakeProvider] = {}
    if gemini is not None:
        providers[Provider.GEMINI] = gemini
    if anthropic is not None:
        providers[Provider.ANTHROPIC] = anthropic
    # backoff_base=0 exercises the real retry path without real sleeps.
    return LLMClient(ProviderRegistry(providers), settings=settings, backoff_base=0.0)


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _turn(text: str = "", calls: list[ToolCall] | None = None) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason="end_turn" if not calls else "tool_use",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _registry_with(*descriptors: ToolDescriptor) -> ToolRegistry:
    registry = ToolRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry


def _echo_tool(calls: list[dict[str, Any]] | None = None) -> ToolDescriptor:
    async def handler(text: str) -> str:
        if calls is not None:
            calls.append({"text": text})
        return f"echo: {text}"

    return ToolDescriptor(
        name="echo",
        description="Echo text back.",
        input_schema=ECHO_SCHEMA,
        category="test",
        handler=handler,
    )


def _costly_turn(calls: list[ToolCall] | None = None, *, output_tokens: int = 50_000) -> AgentTurn:
    return AgentTurn(
        text="working",
        tool_calls=calls or [],
        stop_reason="end_turn" if not calls else "tool_use",
        provider=Provider.ANTHROPIC,
        model="claude-opus-5",
        usage=Usage(input_tokens=50_000, output_tokens=output_tokens),
    )


def _big_tool(size: int) -> ToolDescriptor:
    async def handler() -> str:
        return "x" * size

    return ToolDescriptor(
        name="big",
        description="Returns a large blob.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="test",
        handler=handler,
    )


async def test_a_run_stops_once_it_has_spent_its_cost_cap(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """`max_steps` alone is a poor proxy for spend: an agent re-sends its whole
    transcript every step, so a run with large tool results can cost several times
    another run of the same length. Without a dollar-denominated stop, a stubborn
    repair could cost multiples of the full regeneration it exists to undercut."""
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    # Each step bills ~$1.50 at Opus-5 pricing (50k in @ $5/Mtok, 50k out @ $25/Mtok).
    gemini_fake.register_turns(*[_costly_turn([call]) for _ in range(8)])
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(
        objective="Fix it.",
        system="s",
        tool_categories=["test"],
        max_steps=8,
        cost_cap_usd=2.0,
        cost_model="claude-opus-5",
    )

    assert result.stopped_because == "cost_cap"
    assert result.steps < 8, "the cap must stop the run before its step ceiling"


async def test_a_cheap_run_is_never_stopped_by_the_cost_cap(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    gemini_fake.register_turns(_turn(text="done"))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(
        objective="Fix it.",
        system="s",
        tool_categories=["test"],
        cost_cap_usd=1.0,
        cost_model="claude-opus-5",
    )

    assert result.stopped_because == "completed"


async def test_no_cost_cap_leaves_the_run_governed_only_by_steps(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """The cap is opt-in: every existing caller passes nothing and must be unaffected."""
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    gemini_fake.register_turns(*[_costly_turn([call]) for _ in range(3)])
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(
        objective="Fix it.", system="s", tool_categories=["test"], max_steps=3
    )

    assert result.stopped_because == "step_limit"
    assert result.steps == 3


# --- context-saturation budget: _compact_to_budget, the pure function ----------------


def test_turn_chars_counts_text_call_arguments_and_result_content() -> None:
    turn = Turn(
        role="assistant",
        text="hello",
        tool_calls=[ToolCall(id="1", name="t", arguments={"a": "bb"})],
    )
    assert _turn_chars(turn) == len("hello") + len(json.dumps({"a": "bb"}))


def test_context_chars_sums_every_turn() -> None:
    turns = [Turn(role="user", text="ab"), Turn(role="assistant", text="cde")]
    assert _context_chars(turns) == 5


def test_compact_to_budget_drops_the_oldest_droppable_tool_result_first() -> None:
    turns = [
        Turn(role="user", text="objective"),
        Turn(role="assistant", tool_calls=[ToolCall(id="1", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="1", name="big", content="x" * 1000)]),
        Turn(role="assistant", tool_calls=[ToolCall(id="2", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="2", name="big", content="y" * 1000)]),
    ]

    _compact_to_budget(turns, max_context_chars=1200)

    assert turns[0].text == "objective"  # the objective is never touched
    assert "elided" in turns[2].tool_results[0].content
    assert turns[2].tool_results[0].content != "x" * 1000
    assert turns[3].tool_calls[0].id == "2"  # the protected tail is untouched
    assert turns[4].tool_results[0].content == "y" * 1000


def test_compact_to_budget_stops_dropping_once_back_under_budget() -> None:
    """Oldest-first, and no further than necessary: a second droppable turn must
    survive if dropping only the first one already satisfies the budget."""
    turns = [
        Turn(role="user", text="o"),
        Turn(role="assistant", tool_calls=[ToolCall(id="1", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="1", name="big", content="x" * 1000)]),
        Turn(role="assistant", tool_calls=[ToolCall(id="2", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="2", name="big", content="y" * 1000)]),
        Turn(role="assistant", tool_calls=[ToolCall(id="3", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="3", name="big", content="z" * 1000)]),
    ]

    _compact_to_budget(turns, max_context_chars=2100)

    assert "elided" in turns[2].tool_results[0].content
    assert turns[4].tool_results[0].content == "y" * 1000
    assert turns[6].tool_results[0].content == "z" * 1000


def test_compact_to_budget_is_a_no_op_when_nothing_is_droppable() -> None:
    """Only the objective and the single most recent step exist -- both are
    protected, so a budget too tight to ever satisfy must leave everything
    exactly as it found it rather than violate the protection."""
    turns = [
        Turn(role="user", text="objective"),
        Turn(role="assistant", tool_calls=[ToolCall(id="1", name="big", arguments={})]),
        Turn(role="user", tool_results=[ToolResult(call_id="1", name="big", content="x" * 1000)]),
    ]
    before = list(turns)

    _compact_to_budget(turns, max_context_chars=10)

    assert turns == before


# --- context-saturation budget: AgentRuntime.run integration --------------------------


async def test_no_context_budget_leaves_the_run_governed_only_by_step_limit(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """The budget is opt-in: every existing caller passes nothing and must be
    unaffected, no matter how large the transcript grows."""
    call = ToolCall(id="1", name="big", arguments={})
    gemini_fake.register_turns(*[_turn(calls=[call]) for _ in range(3)])
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_big_tool(50_000)),
    )

    result = await runtime.run(
        objective="Fix it.",
        system="s",
        tool_categories=["test"],
        max_steps=3,
        max_result_chars=200_000,  # otherwise the default cap truncates "big" itself
    )

    assert result.stopped_because == "step_limit"
    assert result.peak_context_chars > 100_000  # tracked even though nothing enforces it


async def test_a_tight_context_budget_compacts_instead_of_ending_the_run(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """Without compaction, three 5,000-char tool results plus the transcript
    overhead around them blow well past a 12,000-char budget -- this proves the
    run recovers and reaches its own natural completion rather than being cut
    off the instant it saturates."""
    call = ToolCall(id="1", name="big", arguments={})
    gemini_fake.register_turns(
        _turn(calls=[call]),
        _turn(calls=[call]),
        _turn(calls=[call]),
        _turn(text="done"),
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_big_tool(5_000)),
    )

    result = await runtime.run(
        objective="Fix it.",
        system="s",
        tool_categories=["test"],
        max_steps=8,
        max_context_chars=12_000,
    )

    assert result.succeeded
    assert result.stopped_because == "completed"
    # Proof this was compaction recovering, not the budget simply never being
    # exceeded: the peak (recorded before compaction each step) really did cross it.
    assert result.peak_context_chars > 12_000


async def test_context_budget_stops_the_run_when_nothing_can_be_dropped(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A budget too tight to satisfy even by dropping everything droppable (here,
    the very first step's own result already exceeds it) must not spin forever --
    it stops cleanly, the same shape as `cost_cap`."""
    call = ToolCall(id="1", name="big", arguments={})
    gemini_fake.register_turns(*[_turn(calls=[call]) for _ in range(5)])
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_big_tool(5_000)),
    )

    result = await runtime.run(
        objective="Fix it.",
        system="s",
        tool_categories=["test"],
        max_steps=5,
        max_context_chars=10,
    )

    assert result.stopped_because == "context_budget"
    assert result.steps == 1  # the first step's own result is what tripped it


async def test_runtime_completes_without_calling_any_tool(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    gemini_fake.register_turns(_turn(text="The answer is 42."))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(objective="What is the answer?", system="s", tool_categories=[])

    assert result.succeeded
    assert result.answer == "The answer is 42."
    assert result.steps == 1
    assert result.invocations == []


async def test_an_empty_answer_with_no_tool_calls_is_not_success(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """Regression test: previously any turn with no tool calls was "completed"
    regardless of whether it actually produced an answer -- an empty response
    (e.g. adaptive thinking consuming the whole output budget) counted as success,
    so a maintenance cycle that produced nothing logged "done", and a caller
    gating a memory write on `result.succeeded` had no signal to stop it."""
    gemini_fake.register_turns(
        AgentTurn(
            text="",
            tool_calls=[],
            stop_reason="end_turn",
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=500, output_tokens=0),
        )
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake), settings=settings, tools=_registry_with()
    )

    result = await runtime.run(objective="Investigate.", system="s", tool_categories=[])

    assert result.succeeded is False
    assert result.stopped_because == "empty_response"


async def test_a_max_tokens_truncated_answer_is_not_success(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A response cut off by hitting the token budget mid-thought must not be
    treated as a finished, trustworthy answer -- it was previously written into
    permanent memory as a "lesson" the moment it wasn't empty."""
    gemini_fake.register_turns(
        AgentTurn(
            text="Based on my analysis, the root cause appea",
            tool_calls=[],
            stop_reason="MAX_TOKENS",
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=500, output_tokens=8192),
        )
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake), settings=settings, tools=_registry_with()
    )

    result = await runtime.run(objective="Investigate.", system="s", tool_categories=[])

    assert result.succeeded is False
    assert result.stopped_because == "truncated"
    # The (untrustworthy) partial text is still surfaced for logging/debugging --
    # only the success signal changes, not what's visible in the result.
    assert "root cause" in result.answer


async def test_a_normal_completed_answer_with_a_clean_stop_reason_still_succeeds(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """Regression guard for the fix above: a genuinely finished answer must not be
    misclassified now that stop_reason is actually being checked."""
    gemini_fake.register_turns(_turn(text="The answer is 42."))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake), settings=settings, tools=_registry_with()
    )

    result = await runtime.run(objective="What is the answer?", system="s", tool_categories=[])

    assert result.succeeded is True
    assert result.stopped_because == "completed"


async def test_runtime_executes_a_requested_tool(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    seen: list[dict[str, Any]] = []
    call = ToolCall(id="call_1", name="echo", arguments={"text": "hello"})
    gemini_fake.register_turns(
        _turn(calls=[call]),
        _turn(text="Done: echo: hello"),
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool(seen)),
    )

    result = await runtime.run(objective="Echo hello", system="s", tool_categories=["test"])

    assert result.succeeded
    assert seen == [{"text": "hello"}]
    assert len(result.invocations) == 1
    assert result.invocations[0].ok is True
    assert result.answer == "Done: echo: hello"


async def test_tool_outside_the_allowlist_is_never_offered(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A research agent spawned without the 'test' category cannot reach `echo`,
    regardless of what the model asks for — the tool is never in its payload."""
    seen: list[dict[str, Any]] = []
    gemini_fake.register_turns(_turn(text="I have no tools for this."))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool(seen)),
    )

    await runtime.run(objective="Echo hello", system="s", tool_categories=["other"])

    sent_tools = gemini_fake.conversations[0]["tools"]
    assert sent_tools == []
    assert seen == []


async def test_unknown_tool_name_returns_an_error_result_not_a_crash(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A hallucinated tool name is something the model should see and correct."""
    bogus_call = ToolCall(id="call_1", name="does_not_exist", arguments={})
    gemini_fake.register_turns(
        _turn(calls=[bogus_call]),
        _turn(text="Understood, trying something else."),
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(objective="Do something", system="s", tool_categories=["test"])

    assert result.succeeded
    assert result.invocations[0].ok is False
    assert "does_not_exist" in result.invocations[0].detail
    # The loop continued and produced a final answer rather than raising.
    assert result.answer == "Understood, trying something else."


async def test_a_failing_tool_produces_an_error_result_not_an_exception(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    async def boom() -> str:
        raise RuntimeError("simulated tool failure")

    descriptor = ToolDescriptor(
        name="boom",
        description="always fails",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="test",
        handler=boom,
    )
    gemini_fake.register_turns(
        _turn(calls=[ToolCall(id="c1", name="boom", arguments={})]),
        _turn(text="It failed, reporting that."),
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(descriptor),
    )

    result = await runtime.run(objective="Trigger the bug", system="s", tool_categories=["test"])

    assert result.succeeded
    assert result.invocations[0].ok is False
    assert "simulated tool failure" in result.invocations[0].detail


async def test_step_limit_stops_an_agent_that_never_finishes(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """An agent that keeps calling tools forever must stop, not run forever."""
    call = ToolCall(id="c1", name="echo", arguments={"text": "again"})
    for _ in range(10):
        gemini_fake.register_turns(_turn(calls=[call]))

    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(
        objective="Loop forever", system="s", tool_categories=["test"], max_steps=3
    )

    assert result.stopped_because == "step_limit"
    assert result.steps == 3
    assert len(result.invocations) == 3


async def test_deadline_stops_an_agent_before_the_step_limit(
    gemini_fake: FakeProvider, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung tool cannot pin a worker forever, even within its step budget."""
    call = ToolCall(id="c1", name="echo", arguments={"text": "slow"})
    for _ in range(10):
        gemini_fake.register_turns(_turn(calls=[call]))

    original_monotonic = time.monotonic
    calls = {"n": 0}

    def fake_monotonic() -> float:
        calls["n"] += 1
        # First call establishes `started`; every call after appears to be far in
        # the future, so the very next deadline check trips.
        return original_monotonic() + (0 if calls["n"] == 1 else 10_000)

    monkeypatch.setattr("factory.agents.runtime.time.monotonic", fake_monotonic)
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(
        objective="Loop forever",
        system="s",
        tool_categories=["test"],
        max_steps=10,
        deadline_seconds=5,
    )

    # The deadline is checked before the step counter advances, so a deadline that
    # trips on the very first check stops the run having recorded zero steps.
    assert result.stopped_because == "deadline"
    assert result.steps == 0


async def test_deadline_is_also_checked_between_tool_calls_within_one_step(
    gemini_fake: FakeProvider, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the deadline used to be checked only once per step, before
    the LLM call -- a single turn requesting several tool calls (each individually
    allowed up to 120s) could run far past the deadline before the *next* step's
    check ever fired. Registers one turn asking for 3 tool calls; the deadline
    trips after the first one actually runs."""
    calls = [
        ToolCall(id="c1", name="echo", arguments={"text": "one"}),
        ToolCall(id="c2", name="echo", arguments={"text": "two"}),
        ToolCall(id="c3", name="echo", arguments={"text": "three"}),
    ]
    gemini_fake.register_turns(_turn(calls=calls))

    original_monotonic = time.monotonic
    check_count = {"n": 0}

    def fake_monotonic() -> float:
        check_count["n"] += 1
        # 1st call: `started`. 2nd: the outer per-step check (must pass). 3rd: the
        # first inner per-tool-call check (must pass, so call 1 actually runs).
        # 4th onward: far future, so the check before call 2 trips.
        return original_monotonic() + (0 if check_count["n"] <= 3 else 10_000)

    monkeypatch.setattr("factory.agents.runtime.time.monotonic", fake_monotonic)
    executed: list[dict[str, Any]] = []
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool(executed)),
    )

    result = await runtime.run(
        objective="Run three tools",
        system="s",
        tool_categories=["test"],
        max_steps=5,
        deadline_seconds=5,
    )

    assert result.stopped_because == "deadline"
    # The step counter had already advanced (the outer check passed), but only the
    # first of the three requested tool calls actually ran before the mid-step
    # deadline check stopped the loop.
    assert result.steps == 1
    assert len(executed) == 1
    assert executed[0]["text"] == "one"


async def test_provider_refusal_propagates_rather_than_being_swallowed(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A policy decline is a decision for a human, not something to retry silently."""
    gemini_fake.register_turns(ProviderRefusal(Provider.GEMINI, "SAFETY", "declined"))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake), settings=settings, tools=ToolRegistry()
    )

    with pytest.raises(ProviderRefusal):
        await runtime.run(objective="Do something risky", system="s", tool_categories=[])


async def test_provider_outage_stops_the_agent_without_raising(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A transport failure ends the run cleanly; the caller decides what to do next.

    `LLMClient.converse` retries a transport failure up to 3 times against the same
    provider before giving up (mirroring `parse`), so with only Gemini registered and
    no failover partner, three failures must be queued to exhaust that retry budget.
    """
    for _ in range(3):
        gemini_fake.register_turns(ProviderUnavailable(Provider.GEMINI, "503", status=503))
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake), settings=settings, tools=ToolRegistry()
    )

    result = await runtime.run(objective="Do something", system="s", tool_categories=[])

    assert result.stopped_because == "provider_error"
    assert not result.succeeded


async def test_no_provider_available_reports_rather_than_raises(settings: Settings) -> None:
    runtime = AgentRuntime(_client(settings), settings=settings, tools=ToolRegistry())
    result = await runtime.run(
        objective="Do something", system="s", tool_categories=[], task_type=TaskType.ANALYSIS
    )
    assert result.stopped_because == "no_provider"
    assert result.steps == 0


async def test_usage_accumulates_across_steps(
    gemini_fake: FakeProvider, settings: Settings
) -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    gemini_fake.register_turns(
        _turn(calls=[call]),
        _turn(text="done"),
    )
    runtime = AgentRuntime(
        _client(settings, gemini=gemini_fake),
        settings=settings,
        tools=_registry_with(_echo_tool()),
    )

    result = await runtime.run(objective="x", system="s", tool_categories=["test"])

    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 10


def test_available_tools_filters_by_category_and_environment(settings: Settings) -> None:
    sandboxed = ToolDescriptor(
        name="run",
        description="d",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="sandbox",
        handler=_echo_tool().handler,
        requires_sandbox=True,
    )
    runtime = AgentRuntime(
        _client(settings), settings=settings, tools=_registry_with(sandboxed, _echo_tool())
    )

    assert runtime.available_tools(["sandbox"], docker_available=False) == []
    assert [d.name for d in runtime.available_tools(["sandbox"], docker_available=True)] == ["run"]
    assert [d.name for d in runtime.available_tools(["test"], docker_available=False)] == ["echo"]


async def test_runtime_renders_tools_in_the_serving_providers_shape(
    gemini_fake: FakeProvider,
    anthropic_fake: FakeProvider,
    settings: Settings,
) -> None:
    """The payload format depends on which provider actually answers the turn, not
    on which one was merely configured — code generation is routed to Claude, so
    that run must see `input_schema`, never `parameters`."""
    gemini_fake.register_turns(_turn(text="done"))
    anthropic_fake.register_turns(_turn(text="done"))
    client = _client(settings, gemini=gemini_fake, anthropic=anthropic_fake)
    runtime = AgentRuntime(client, settings=settings, tools=_registry_with(_echo_tool()))

    await runtime.run(
        objective="x", system="s", tool_categories=["test"], task_type=TaskType.ANALYSIS
    )
    await runtime.run(
        objective="x", system="s", tool_categories=["test"], task_type=TaskType.CODE_GENERATION
    )

    gemini_tools = gemini_fake.conversations[0]["tools"]
    anthropic_tools = anthropic_fake.conversations[0]["tools"]
    assert "parameters" in gemini_tools[0]
    assert "input_schema" not in gemini_tools[0]
    assert anthropic_tools[0]["input_schema"]["additionalProperties"] is False
    assert anthropic_tools[0]["strict"] is True
