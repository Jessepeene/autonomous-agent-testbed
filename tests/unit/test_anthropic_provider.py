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
