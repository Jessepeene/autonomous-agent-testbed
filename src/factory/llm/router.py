"""Task-type → provider routing.

A static dict, never a model decision. Getting this wrong would mean code silently
written by the wrong model, so the mapping is data that a test can assert over.
"""

from __future__ import annotations

from factory.llm.providers.base import LLMProvider, Provider
from factory.settings import Settings, TaskType, get_settings

#: Gemini handles general reasoning; Claude handles anything that produces or
#: judges source code via strict structured output; DeepSeek handles agentic
#: repair/healing (tool-calling only -- see TaskType.CODE_REPAIR's docstring in
#: settings.py for why it's split out from CODE_GENERATION rather than sharing it).
ROUTING: dict[TaskType, Provider] = {
    TaskType.ORCHESTRATION: Provider.GEMINI,
    TaskType.DISCOVERY: Provider.GEMINI,
    TaskType.ANALYSIS: Provider.GEMINI,
    TaskType.COMPLIANCE: Provider.GEMINI,
    TaskType.MARKETING_COPY: Provider.GEMINI,
    TaskType.CODE_GENERATION: Provider.ANTHROPIC,
    TaskType.CODE_REVIEW: Provider.ANTHROPIC,
    TaskType.CODE_REPAIR: Provider.DEEPSEEK,
}

#: Code tasks must never be silently served by the other vendor: a cross-provider
#: failover here would change *who wrote the code that ships*, invisibly.
NO_FAILOVER: frozenset[TaskType] = frozenset(
    {TaskType.CODE_GENERATION, TaskType.CODE_REVIEW, TaskType.CODE_REPAIR}
)


def provider_for(task_type: TaskType) -> Provider:
    try:
        return ROUTING[task_type]
    except KeyError as exc:  # pragma: no cover - unreachable while the enum is exhaustive
        raise KeyError(f"no provider configured for task type {task_type!r}") from exc


def may_failover(task_type: TaskType) -> bool:
    return task_type not in NO_FAILOVER


def model_for(task_type: TaskType, settings: Settings | None = None) -> str:
    """Model id per task type.

    Orchestration runs on every tick and produces a tightly-constrained decision, so
    it uses the fast model; everything judgement-heavy uses the general model.
    """
    settings = settings or get_settings()
    if task_type is TaskType.ORCHESTRATION:
        return settings.model_fast
    provider = provider_for(task_type)
    if provider is Provider.ANTHROPIC:
        return settings.model_code
    if provider is Provider.DEEPSEEK:
        return settings.model_deepseek
    return settings.model_general


def max_tokens_for(task_type: TaskType) -> int:
    if task_type is TaskType.CODE_GENERATION:
        # 32_000 was observed live to be too tight: Claude Sonnet 5's adaptive
        # thinking is not budget-capped (budget_tokens is rejected outright on this
        # model), and an open-ended "produce the complete MVP" prompt let thinking
        # alone consume the entire 32k, hitting stop_reason="max_tokens" with a
        # single "thinking" content block and zero answer text -- every build_mvp
        # attempt failed with SchemaViolation("claude returned no text content")
        # this way. 64_000 is Claude Sonnet 5's supported ceiling (confirmed against
        # the live API) and the only lever available to give thinking and the final
        # JSON answer enough combined room.
        return 64_000
    if task_type is TaskType.CODE_REVIEW:
        return 16_000
    if task_type is TaskType.CODE_REPAIR:
        # Same ceiling as CODE_GENERATION -- WORKSPACE_SURGEON/SEO_OPTIMIZER/
        # REPAIR_ENGINEER got this budget when they were still routed under
        # CODE_GENERATION; splitting the task type out is a routing change,
        # not a reason to also shrink their existing headroom.
        return 64_000
    if task_type is TaskType.ORCHESTRATION:
        return 2_048
    return 8_192


class ProviderRegistry:
    """Holds one instance per provider and answers availability questions."""

    def __init__(self, providers: dict[Provider, LLMProvider]) -> None:
        self._providers = providers

    def get(self, provider: Provider) -> LLMProvider | None:
        instance = self._providers.get(provider)
        if instance is None or not instance.available:
            return None
        return instance

    def alternatives(self, exclude: Provider) -> list[LLMProvider]:
        return [p for name, p in self._providers.items() if name is not exclude and p.available]

    def available_providers(self) -> list[Provider]:
        return [name for name, p in self._providers.items() if p.available]

    def items(self) -> list[tuple[Provider, LLMProvider]]:
        """Every configured provider, available or not — used to wrap each one
        identically (e.g. for metering) without caring which are currently live."""
        return list(self._providers.items())
