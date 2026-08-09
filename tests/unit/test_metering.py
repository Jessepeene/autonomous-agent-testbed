"""Call metering — the minimum needed to answer "did this change actually reduce
tokens" with a number rather than an assertion. No tokenizer, no network: character
counts of the exact wire payload are exact by construction."""

from __future__ import annotations

import pytest

from factory.llm.providers.base import (
    AgentTurn,
    Provider,
    ProviderUnavailable,
    ToolCall,
    ToolResult,
    Turn,
    Usage,
)
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.observability.metering import (
    CallRecord,
    MeteringProvider,
    estimate_tokens,
    metered_registry,
)
from factory.settings import TaskType
from factory.state.schemas import IdeaBatch


async def test_parse_records_exact_payload_character_counts() -> None:
    fake = FakeProvider(Provider.GEMINI)
    fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    sink: list[CallRecord] = []
    metered = MeteringProvider(fake, sink)

    await metered.parse(
        IdeaBatch,
        system="0123456789",
        prompt="abcde",
        task_type=TaskType.DISCOVERY,
        model="gemini-2.5-pro",
    )

    assert len(sink) == 1
    record = sink[0]
    assert record.method == "parse"
    assert record.system_chars == 10
    assert record.input_chars == 5
    assert record.tools_chars == 0
    assert record.ok is True
    assert record.payload_chars == 15


async def test_converse_sums_characters_across_every_turn() -> None:
    fake = FakeProvider(Provider.GEMINI)
    fake.register_turns(
        AgentTurn(
            text="ok",
            tool_calls=[],
            stop_reason="end_turn",
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            usage=Usage(),
        )
    )
    sink: list[CallRecord] = []
    metered = MeteringProvider(fake, sink)
    turns = [
        Turn(role="user", text="12345"),
        Turn(
            role="assistant",
            text="",
            tool_calls=[ToolCall(id="c1", name="tool", arguments={"a": "b"})],
        ),
        Turn(
            role="user",
            tool_results=[ToolResult(call_id="c1", name="tool", content="0123456789")],
        ),
    ]

    await metered.converse(
        system="sys",
        turns=turns,
        tools=[],
        task_type=TaskType.ANALYSIS,
        model="gemini-2.5-flash",
    )

    import json as json_module

    assert len(sink) == 1
    record = sink[0]
    assert record.method == "converse"
    call_args_chars = len(json_module.dumps({"a": "b"}, sort_keys=True, default=str))
    assert record.input_chars == len("12345") + len("tool") + call_args_chars + len("0123456789")
    assert record.output_chars == 2  # "ok"


async def test_a_failed_call_is_still_recorded_as_not_ok() -> None:
    fake = FakeProvider(Provider.GEMINI)
    fake.register(IdeaBatch, ProviderUnavailable(Provider.GEMINI, "503", status=503))
    sink: list[CallRecord] = []
    metered = MeteringProvider(fake, sink)

    with pytest.raises(ProviderUnavailable):
        await metered.parse(
            IdeaBatch, system="s", prompt="p", task_type=TaskType.DISCOVERY, model="m"
        )

    assert len(sink) == 1
    assert sink[0].ok is False
    assert sink[0].error is not None


async def test_tools_payload_is_counted_once_as_its_serialized_size() -> None:
    fake = FakeProvider(Provider.GEMINI)
    fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    sink: list[CallRecord] = []
    metered = MeteringProvider(fake, sink)
    tools = [{"name": "http_get", "description": "d"}]

    await metered.parse(
        IdeaBatch, system="s", prompt="p", task_type=TaskType.DISCOVERY, model="m", tools=tools
    )

    import json

    assert sink[0].tools_chars == len(json.dumps(tools, sort_keys=True, default=str))


def test_token_estimate_uses_the_content_kind_divisor() -> None:
    assert estimate_tokens(380, "prose") == 100
    assert estimate_tokens(300, "json") == 100
    assert estimate_tokens(100, "unknown-kind") == round(100 / 3.8)


async def test_metered_registry_wraps_every_configured_provider() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    anthropic = FakeProvider(Provider.ANTHROPIC)
    sink: list[CallRecord] = []
    wrapped = metered_registry(
        ProviderRegistry({Provider.GEMINI: gemini, Provider.ANTHROPIC: anthropic}), sink
    )

    assert isinstance(wrapped.get(Provider.GEMINI), MeteringProvider)
    assert isinstance(wrapped.get(Provider.ANTHROPIC), MeteringProvider)

    gemini.register(IdeaBatch, IdeaBatch(ideas=[]))
    await wrapped.get(Provider.GEMINI).parse(  # type: ignore[union-attr]
        IdeaBatch, system="s", prompt="p", task_type=TaskType.DISCOVERY, model="m"
    )
    assert len(sink) == 1
