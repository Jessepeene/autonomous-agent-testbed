"""Deterministic provider for tests.

Lets the whole factory — supervisor, engines, guardrails — be exercised with no
network, no API key, and no cost, while still returning schema-valid contracts.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, TypeVar

from pydantic import BaseModel

from factory.llm.providers.base import AgentTurn, Completion, LLMError, Provider, Turn, Usage
from factory.settings import TaskType

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class FakeProvider:
    """Returns queued responses per contract type, in FIFO order."""

    def __init__(self, name: Provider = Provider.GEMINI) -> None:
        self.name = name
        self._queues: dict[type[BaseModel], deque[BaseModel | LLMError]] = defaultdict(deque)
        self._turns: deque[AgentTurn | LLMError] = deque()
        self.calls: list[dict[str, Any]] = []
        self.conversations: list[dict[str, Any]] = []
        self.available_flag = True

    @property
    def available(self) -> bool:
        return self.available_flag

    def register(self, schema: type[BaseModel], *values: BaseModel | LLMError) -> None:
        for value in values:
            self._queues[schema].append(value)

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
        self.calls.append(
            {
                "schema": schema.__name__,
                "task_type": task_type,
                "model": model,
                "system": system,
                "prompt": prompt,
                "tools": tools,
                "cache_prefix": cache_prefix,
            }
        )
        queue = self._queues[schema]
        if not queue:
            raise AssertionError(
                f"FakeProvider has no queued response for {schema.__name__}; "
                "register one in the test"
            )
        item = queue.popleft()
        if isinstance(item, LLMError):
            raise item
        assert isinstance(item, schema)
        return Completion(
            value=item,
            provider=self.name,
            model=model,
            usage=Usage(input_tokens=10, output_tokens=10, latency_ms=1),
        )

    def register_turns(self, *turns: AgentTurn | LLMError) -> None:
        for turn in turns:
            self._turns.append(turn)

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
        self.conversations.append(
            {"system": system, "turns": list(turns), "tools": tools, "model": model}
        )
        if not self._turns:
            raise AssertionError("FakeProvider has no queued agent turn; register one")
        item = self._turns.popleft()
        if isinstance(item, LLMError):
            raise item
        return item

    async def list_models(self) -> list[str]:
        return [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5",
        ]
