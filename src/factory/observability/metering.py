"""Lightweight LLM call metering.

Wraps any ``LLMProvider`` and records the exact wire-payload size of every call — no
tokenizer, no network call, no extra dependency. Character counts are exact; the
token estimate is a documented approximation. Treat *ratios* between two metered runs
(before vs. after a change) as reliable; treat the absolute token/dollar figures as a
rough, labeled guess.

Deliberately not the full benchmark harness this could grow into (six scripted
scenarios, a differential outcome digest, an ``--exact`` mode calibrated against
Anthropic's ``count_tokens`` endpoint) — this is the minimum that lets a change like
"does deterministic-first routing actually cut tokens" or "does a compact encoding
actually help" be answered with a real number instead of an assertion.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from factory.llm.providers.base import Completion, Image, LLMProvider, Turn
from factory.llm.router import ProviderRegistry
from factory.settings import TaskType

SchemaT = TypeVar("SchemaT", bound=BaseModel)

#: Rough chars-per-token by content kind, used only for the headline estimate columns.
#: The character counts themselves — what a before/after comparison should actually
#: rely on — are exact regardless of this table.
CHARS_PER_TOKEN: dict[str, float] = {"prose": 3.8, "json": 3.0, "code": 3.3}


def estimate_tokens(chars: int, kind: str = "prose") -> int:
    return round(chars / CHARS_PER_TOKEN.get(kind, 3.8))


@dataclass(slots=True)
class CallRecord:
    seq: int
    method: str  # "parse" | "converse"
    provider: str
    model: str
    task_type: str
    system_chars: int
    input_chars: int  # the prompt (parse) or serialized turns (converse)
    tools_chars: int
    output_chars: int
    latency_ms: int
    ok: bool
    error: str | None = None
    cache_prefix_chars: int = 0

    @property
    def payload_chars(self) -> int:
        """Everything actually sent on the wire for this call."""
        return self.system_chars + self.input_chars + self.tools_chars + self.cache_prefix_chars


def _tools_chars(tools: list[dict[str, Any]] | None) -> int:
    return len(json.dumps(tools, sort_keys=True, default=str)) if tools else 0


def _turn_chars(turn: Turn) -> int:
    total = len(turn.text)
    for call in turn.tool_calls:
        total += len(call.name) + len(json.dumps(call.arguments, sort_keys=True, default=str))
    for result in turn.tool_results:
        total += len(result.content)
    return total


class MeteringProvider:
    """Structurally satisfies ``LLMProvider``. Every call, success or failure, adds
    exactly one ``CallRecord`` to ``sink``."""

    def __init__(self, inner: LLMProvider, sink: list[CallRecord]) -> None:
        self._inner = inner
        self._sink = sink
        self._seq = 0
        # A plain attribute, not a property: the Protocol declares `name` as a
        # settable variable (matching GeminiProvider/AnthropicProvider), and it never
        # changes for the lifetime of a wrapped provider anyway.
        self.name = inner.name

    @property
    def available(self) -> bool:
        return self._inner.available

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
        images: list[Image] | None = None,
    ) -> Completion[SchemaT]:
        self._seq += 1
        seq = self._seq
        tools_chars = _tools_chars(tools)
        cache_prefix_chars = len(cache_prefix) if cache_prefix else 0
        started = time.perf_counter()
        try:
            completion = await self._inner.parse(
                schema,
                system=system,
                prompt=prompt,
                task_type=task_type,
                model=model,
                max_tokens=max_tokens,
                tools=tools,
                cache_prefix=cache_prefix,
                images=images,
            )
        except Exception as exc:
            self._record(
                seq,
                "parse",
                model,
                task_type,
                len(system),
                len(prompt),
                tools_chars,
                0,
                started,
                ok=False,
                error=str(exc)[:200],
                cache_prefix_chars=cache_prefix_chars,
            )
            raise
        output_chars = len(completion.value.model_dump_json())
        self._record(
            seq,
            "parse",
            model,
            task_type,
            len(system),
            len(prompt),
            tools_chars,
            output_chars,
            started,
            ok=True,
            cache_prefix_chars=cache_prefix_chars,
        )
        return completion

    async def converse(
        self,
        *,
        system: str,
        turns: list[Turn],
        tools: list[dict[str, Any]],
        task_type: TaskType,
        model: str,
        max_tokens: int = 8192,
    ) -> Any:
        self._seq += 1
        seq = self._seq
        turns_chars = sum(_turn_chars(t) for t in turns)
        tools_chars = _tools_chars(tools)
        started = time.perf_counter()
        try:
            turn = await self._inner.converse(
                system=system,
                turns=turns,
                tools=tools,
                task_type=task_type,
                model=model,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self._record(
                seq,
                "converse",
                model,
                task_type,
                len(system),
                turns_chars,
                tools_chars,
                0,
                started,
                ok=False,
                error=str(exc)[:200],
            )
            raise
        self._record(
            seq,
            "converse",
            model,
            task_type,
            len(system),
            turns_chars,
            tools_chars,
            len(turn.text),
            started,
            ok=True,
        )
        return turn

    async def list_models(self) -> list[str]:
        return await self._inner.list_models()

    def _record(
        self,
        seq: int,
        method: str,
        model: str,
        task_type: TaskType,
        system_chars: int,
        input_chars: int,
        tools_chars: int,
        output_chars: int,
        started: float,
        *,
        ok: bool,
        error: str | None = None,
        cache_prefix_chars: int = 0,
    ) -> None:
        self._sink.append(
            CallRecord(
                seq=seq,
                method=method,
                provider=str(self._inner.name),
                model=model,
                task_type=str(task_type),
                system_chars=system_chars,
                input_chars=input_chars,
                tools_chars=tools_chars,
                output_chars=output_chars,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=ok,
                error=error,
                cache_prefix_chars=cache_prefix_chars,
            )
        )


def metered_registry(registry: ProviderRegistry, sink: list[CallRecord]) -> ProviderRegistry:
    """Wrap every provider in ``registry`` identically, so metering never depends on
    knowing which vendors happen to be configured."""
    return ProviderRegistry(
        {name: MeteringProvider(provider, sink) for name, provider in registry.items()}
    )
