"""Provider-agnostic model interface.

Engines depend on this Protocol, never on a vendor SDK. Swapping which provider
serves a task is a one-line change in ``router.py``, not an edit to any engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from factory.settings import TaskType

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class Provider(StrEnum):
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


class LLMError(RuntimeError):
    """Base for every provider failure."""


class ProviderRefusal(LLMError):
    """The model declined on policy grounds.

    Retrying the same prompt will not help, and falling back to another provider is a
    policy-laundering move we deliberately do not make. Escalate instead.
    """

    def __init__(self, provider: Provider, category: str | None, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.category = category


class ProviderUnavailable(LLMError):
    """Transport, quota, or 5xx failure. Retryable, and may fail over."""

    def __init__(self, provider: Provider, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


class SchemaViolation(LLMError):
    """The model returned something that does not satisfy the contract."""


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0


@dataclass(slots=True)
class Completion[SchemaT: BaseModel]:
    value: SchemaT
    provider: Provider
    model: str
    usage: Usage = field(default_factory=Usage)


# --- tool calling -----------------------------------------------------------
#
# Structured output (`parse`) answers "what do you conclude". Tool calling
# (`converse`) answers "what do you want to do next", which is what an agent needs.
# Both are expressed in provider-neutral shapes so the agent runtime never learns
# whether Claude or Gemini is on the other end.


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Turn:
    """One step of an agent conversation, in provider-neutral form."""

    role: str  # "user" | "assistant"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass(slots=True)
class AgentTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    provider: Provider
    model: str
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """What every provider must implement."""

    name: Provider

    @property
    def available(self) -> bool:
        """False when the API key is absent; the router skips unavailable providers."""
        ...

    async def parse(
        self,
        schema: type[SchemaT],
        *,
        system: str,
        prompt: str,
        task_type: TaskType,
        model: str,
        max_tokens: int = 8192,
        tools: list[dict[str, Any]] | None = None,
        cache_prefix: str | None = None,
    ) -> Completion[SchemaT]:
        """Return an instance of ``schema``, or raise one of the LLMError subclasses.

        ``cache_prefix`` is text that is byte-identical across a fan-out of related
        calls (e.g. the same codebase reviewed by several prompts). Providers with
        explicit prompt caching place it first and mark it cacheable; providers
        without simply prepend it. Semantics are advisory, never load-bearing — a
        provider that ignores it must still produce a correct answer.
        """
        ...

    async def converse(
        self,
        *,
        system: str,
        turns: list[Turn],
        tools: list[dict[str, Any]],
        task_type: TaskType,
        model: str,
        max_tokens: int = 8192,
    ) -> AgentTurn:
        """One agent step: reply, or ask to call tools.

        ``tools`` arrives in this provider's own payload shape — the registry renders
        it, because only the provider knows whether it wants ``input_schema`` or
        ``parameters``.
        """
        ...

    async def list_models(self) -> list[str]:
        """Used by preflight to catch a stale configured model ID before boot."""
        ...
