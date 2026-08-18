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
    DEEPSEEK = "deepseek"


class LLMError(RuntimeError):
    """Base for every provider failure."""


class ProviderRefusal(LLMError):
    """The model declined on policy grounds.

    Retrying the same prompt will not help, and falling back to another provider is a
    policy-laundering move we deliberately do not make. Escalate instead.

    ``usage`` is attached whenever the provider's response carried real token counts
    (a refusal is frequently a successful, billed HTTP 200 -- Claude's refusal
    `stop_reason` in particular). Without it, `client.py`'s cost ledger records a
    real, billed call as 0 tokens / $0, which made the daily spend cap and the
    cost-reduction maintenance agent both blind to this failure mode.
    """

    def __init__(
        self,
        provider: Provider,
        category: str | None,
        message: str,
        *,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.category = category
        self.usage = usage


class ProviderUnavailable(LLMError):
    """Transport, quota, or 5xx failure. Retryable, and may fail over."""

    def __init__(self, provider: Provider, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


class ProviderRequestRejected(ProviderUnavailable):
    """A permanent 4xx (bad request, auth, not found) -- the exact same request will
    never succeed against this provider no matter how many times it is retried, so
    retrying it wastes latency for nothing. Still a `ProviderUnavailable` subclass:
    a different vendor may well accept the same logical request (e.g. one provider's
    JSON Schema dialect rejecting a keyword the other allows), so failover to an
    alternative provider still makes sense -- only the same-provider retry loop
    should be skipped.
    """


class SchemaViolation(LLMError):
    """The model returned something that does not satisfy the contract.

    ``usage`` is attached whenever the provider's response carried real token counts
    -- a schema violation is very often a fully successful, billed generation (e.g.
    a large `thinking` budget consuming the whole output) that simply failed
    Pydantic validation afterward, not a free no-op.
    """

    def __init__(self, message: str, *, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


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
