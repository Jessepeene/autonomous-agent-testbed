"""``cache_prefix`` plumbing.

Additive to `parse()` on the Protocol and every implementation, so all nine existing
call sites keep working unchanged. The one place correctness actually matters is the
Anthropic block ordering: the shared content must be the *first* system block and the
only one marked cacheable, or a fan-out of calls that share it (the Bug Council) will
never actually share a cache entry.
"""

from __future__ import annotations

from factory.llm.client import LLMClient
from factory.llm.providers.anthropic_provider import _system_blocks
from factory.llm.providers.base import Provider
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
