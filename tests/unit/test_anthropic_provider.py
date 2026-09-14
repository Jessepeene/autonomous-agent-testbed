"""AnthropicProvider's own request-shaping logic -- previously untested directly
(only ever exercised indirectly via FakeProvider substitution), which is how a
real production bug shipped unnoticed until it hit live traffic.
"""

from __future__ import annotations

from factory.llm.providers.anthropic_provider import AnthropicProvider
from factory.settings import Settings, TaskType


def _provider() -> AnthropicProvider:
    return AnthropicProvider(Settings(anthropic_api_key="test-key"))


def test_fallback_model_never_gets_an_effort_value() -> None:
    """Real, reproducible regression: model_fallback_anthropic (Haiku 4.5, the
    cheap stand-in used only when a Gemini-routed call fails over) does not
    support the `effort` parameter at all. Confirmed live: a permanent 400
    ("This model does not support the effort parameter") that silently broke
    the entire failover safety net, since Anthropic is the last candidate --
    there is nowhere left to fail over to after this."""
    provider = _provider()
    fallback_model = provider._settings.model_fallback_anthropic

    for task_type in TaskType:
        assert provider._effort(task_type, fallback_model) is None


def test_every_other_model_still_gets_a_real_effort_value() -> None:
    provider = _provider()
    real_model = provider._settings.model_code
    assert real_model != provider._settings.model_fallback_anthropic

    assert provider._effort(TaskType.CODE_GENERATION, real_model) == provider._settings.effort_code
    assert provider._effort(TaskType.CODE_REVIEW, real_model) == provider._settings.effort_review
    assert provider._effort(TaskType.ANALYSIS, real_model) == "high"


def test_a_null_content_field_is_a_provider_failure_not_a_crash() -> None:
    """The bug that took the whole factory down for fifteen minutes.

    DeepSeek returned a 200 whose `content` was null. Every path that read a
    response iterated it directly, so it raised `TypeError: 'NoneType' object is not
    iterable` -- inside the orchestrator's own call, which is the one call every
    tick makes. Every tick died and no engine ran at all.

    The missing content was not the damage; the *kind* of error was. A provider
    returning nothing usable is an ordinary provider failure, and retry and failover
    already handle `SchemaViolation`. A TypeError escapes all of it and kills the
    caller, which is why this is guarded in the shared base class rather than at the
    call sites -- DeepSeek extends this provider, and both inherit the fix.
    """
    from types import SimpleNamespace

    import pytest

    from factory.llm.providers.base import SchemaViolation

    provider = _provider()
    response = SimpleNamespace(content=None, stop_reason="end_turn", usage=None)

    with pytest.raises(SchemaViolation) as caught:
        provider._content_blocks(response, 120)

    assert "no content field" in str(caught.value)


def test_an_empty_content_list_keeps_the_callers_better_message() -> None:
    """Empty is not null, and conflating them would cost a real diagnosis.

    Each caller already has a far better message for an empty response -- naming the
    stop reason and the block types it actually got, which is what distinguishes
    "thinking ate the whole budget" from "ignored a forced tool_choice". A generic
    guard here would replace all of that with nothing.
    """
    from types import SimpleNamespace

    provider = _provider()
    response = SimpleNamespace(content=[], stop_reason="max_tokens", usage=None)

    assert provider._content_blocks(response, 120) == []
