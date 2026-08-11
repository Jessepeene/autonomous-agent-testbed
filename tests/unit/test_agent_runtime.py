"""The agent runtime — the loop that actually executes tools.

Before this loop existed, tools were declared, filtered, and rendered into a
provider payload, and then never invoked: a model could ask for a tool and nothing
would happen. These tests drive the loop with a `FakeProvider` standing in for the
model and assert the containment properties (allowlist, step ceiling, deadline,
unknown-tool handling) that make it safe to hand an LLM a tool belt.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from factory.agents.runtime import AgentRuntime
from factory.llm.client import LLMClient
from factory.llm.providers.base import (
    AgentTurn,
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    ToolCall,
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
