"""Task-type → provider routing.

A static dict, never a model decision. Getting this wrong would mean code silently
written by the wrong model, so the mapping is data that a test can assert over.
"""

from __future__ import annotations

from factory.llm.providers.base import LLMProvider, Provider
from factory.settings import Settings, TaskType, get_settings

#: Gemini handles general reasoning; Claude handles anything that produces or
#: judges source code.
ROUTING: dict[TaskType, Provider] = {
    TaskType.ORCHESTRATION: Provider.GEMINI,
    TaskType.DISCOVERY: Provider.GEMINI,
    TaskType.ANALYSIS: Provider.GEMINI,
    TaskType.COMPLIANCE: Provider.GEMINI,
    TaskType.MARKETING_COPY: Provider.GEMINI,
    TaskType.CODE_GENERATION: Provider.ANTHROPIC,
    TaskType.CODE_REVIEW: Provider.ANTHROPIC,
}

#: Code tasks must never be silently served by the other vendor: a cross-provider
#: failover here would change *who wrote the code that ships*, invisibly.
NO_FAILOVER: frozenset[TaskType] = frozenset({TaskType.CODE_GENERATION, TaskType.CODE_REVIEW})


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
    if provider_for(task_type) is Provider.ANTHROPIC:
        return settings.model_code
    return settings.model_general


def max_tokens_for(task_type: TaskType) -> int:
    if task_type is TaskType.CODE_GENERATION:
        return 32_000
    if task_type is TaskType.CODE_REVIEW:
        return 16_000
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
