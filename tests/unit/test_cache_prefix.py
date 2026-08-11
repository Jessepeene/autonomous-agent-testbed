"""``cache_prefix`` plumbing, and conversation-prefix caching for agent loops.

Additive to `parse()` on the Protocol and every implementation, so all nine existing
call sites keep working unchanged. The one place correctness actually matters is the
Anthropic block ordering: the shared content must be the *first* system block and the
only one marked cacheable, or a fan-out of calls that share it (the Bug Council) will
never actually share a cache entry.

The second half of this file covers the *agent* path, which had a related but
distinct gap: `converse()` cached its system block but nothing else, while the
message transcript -- which is the bulk of the tokens and grows every step -- was
re-sent at full price on every single step. That made a multi-step agent cost more
than the one-shot generation it was meant to replace, which is the entire reason the
surgical repair path exists.
"""

from __future__ import annotations

from typing import Any

from factory.llm.client import LLMClient
from factory.llm.providers.anthropic_provider import _mark_conversation_prefix, _system_blocks
from factory.llm.providers.base import Provider, ToolResult, Turn
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.settings import Settings, TaskType


def test_no_prefix_is_a_single_cacheable_block() -> None:
    blocks = _system_blocks("be a helpful reviewer", None)
    assert blocks == [
        {"type": "text", "text": "be a helpful reviewer", "cache_control": {"type": "ephemeral"}}
    ]


def test_a_shared_prefix_is_first_and_alone_marked_cacheable() -> None:
    blocks = _system_blocks("be a security reviewer", "the whole codebase goes here")
    assert blocks == [
        {
            "type": "text",
            "text": "the whole codebase goes here",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "be a security reviewer"},
    ]


def test_two_different_system_prompts_share_an_identical_cache_prefix_block() -> None:
    """This is the property the Bug Council relies on: reviewers with different
    mandates but the same corpus must produce byte-identical block 0."""
    corpus = "--- app.py ---\nprint('hello')\n"
    security = _system_blocks("You are the security reviewer.", corpus)
    performance = _system_blocks("You are the performance reviewer.", corpus)
    assert security[0] == performance[0]
    assert security[1] != performance[1]


async def test_llm_client_threads_cache_prefix_to_the_provider(settings: Settings) -> None:
    from factory.state.schemas import IdeaBatch

    fake = FakeProvider(Provider.GEMINI)
    fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    client = LLMClient(ProviderRegistry({Provider.GEMINI: fake}), settings=settings)

    await client.parse(
        IdeaBatch,
        task_type=TaskType.DISCOVERY,
        system="s",
        prompt="p",
        cache_prefix="shared-corpus",
    )

    assert fake.calls[0]["cache_prefix"] == "shared-corpus"


# --- conversation-prefix caching (the agent loop) --------------------------------


def _messages(*texts: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": t}]} for t in texts]


def test_a_breakpoint_is_placed_on_the_final_content_block() -> None:
    messages = _mark_conversation_prefix(_messages("step one", "step two"))

    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[0]["content"][-1]


def test_each_step_marks_a_prefix_of_the_next_step() -> None:
    """This is the property that makes caching *incremental* rather than one-shot.
    An agent loop only ever appends, so the prefix marked at step N is by construction
    a strict prefix of step N+1's messages -- which is what lets step N+1 read all of
    it back at a tenth of the price instead of re-paying in full."""
    step_one = _mark_conversation_prefix(_messages("a", "b"))
    step_two = _mark_conversation_prefix(_messages("a", "b", "c"))

    # Everything step one sent is still present, unchanged in content, at the front.
    assert [m["content"][0]["text"] for m in step_two[:2]] == ["a", "b"]
    assert len(step_two) == len(step_one) + 1
    assert step_two[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_a_tool_result_turn_is_markable_too() -> None:
    """Tool results are the bulk of a repair agent's transcript -- a sandbox test run
    can be thousands of tokens -- so they are exactly what must land inside the cached
    prefix rather than outside it."""
    from factory.llm.providers.anthropic_provider import _to_anthropic_message

    turn = Turn(role="user", tool_results=[ToolResult(call_id="1", name="run", content="output")])
    messages = _mark_conversation_prefix([_to_anthropic_message(turn)])

    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_marking_an_empty_conversation_is_a_no_op() -> None:
    assert _mark_conversation_prefix([]) == []
