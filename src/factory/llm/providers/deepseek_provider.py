"""DeepSeek provider -- serves TaskType.CODE_REPAIR only (see llm/router.py).

DeepSeek V4-Pro exposes an Anthropic-compatible endpoint, so this subclasses
AnthropicProvider and overrides only client construction (a different base_url
and a different credential); every request/response shape it inherits --
tool_use blocks, the streaming threshold, retry/refusal/schema-violation
handling -- is identical to the real Claude provider.

Deliberately routed to CODE_REPAIR (`converse()`, agentic tool-calling) and
never to CODE_GENERATION/CODE_REVIEW (`parse()`, strict `output_config.
format=json_schema` structured output): live testing showed DeepSeek's
Anthropic-compatible endpoint does not reliably honor that structured-output
contract -- it returned conversational prose instead of schema-conformant
JSON for a `generate_scaffold()`-shaped call, failing Pydantic validation.
Its tool-calling path, by contrast, was verified against a real broken-test-
suite fixture via the real WORKSPACE_SURGEON role and AgentRuntime loop: it
correctly diagnosed the bug, wrote a minimal correct fix, re-ran the suite,
and reported completion -- independently confirmed passing before this
routing was wired into the live system.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from factory.llm.providers.anthropic_provider import (
    _STREAM_THRESHOLD,
    AnthropicProvider,
    _classify_status_error,
    _system_blocks,
)
from factory.llm.providers.base import (
    Completion,
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    SchemaViolation,
)
from factory.llm.schema import json_schema_for
from factory.settings import TaskType

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_BASE_URL = "https://api.deepseek.com/anthropic"


class DeepSeekProvider(AnthropicProvider):
    name = Provider.DEEPSEEK

    @property
    def available(self) -> bool:
        return self._settings.has_deepseek

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = self._settings.secret("DEEPSEEK_API_KEY")
            if not key:
                raise ProviderUnavailable(self.name, "DEEPSEEK_API_KEY is not set")
            self._client = anthropic.AsyncAnthropic(api_key=key, base_url=_BASE_URL)
        return self._client

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
        """Structured output through a forced tool call, not `output_config`.

        The inherited implementation asks for `output_config.format=json_schema`.
        **DeepSeek's Anthropic-compatible endpoint accepts that parameter and
        ignores it** -- no error, no warning; the schema simply becomes a polite
        suggestion and the model answers in whatever shape it likes. That is why
        this provider was scoped to CODE_REPAIR in the first place (see the module
        docstring), and it is exactly what happened when the POC override routed
        every `parse()` task type here: the Orchestrator emitted `## Verdict`,
        `**target_engine:` and YAML, and produced zero usable decisions across an
        entire afternoon while the factory ran on its deterministic fallback.

        The endpoint *does* honour tool calling -- that path was verified against a
        real broken-test-suite fixture before this provider went live. So the
        contract is expressed as a single tool whose `input_schema` is the schema,
        with `tool_choice` forcing it. The model cannot answer in prose because the
        only move available to it is the call.

        `tools` is ignored deliberately: exactly one tool is offered and
        `tool_choice` requires it, so passing others would give the model a way to
        avoid answering.

        The response carries a `thinking` block alongside the `tool_use` one; the
        scan below ignores anything that is not the result tool.
        """
        tool_name = "emit_result"
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": _system_blocks(system, cache_prefix),
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": tool_name,
                    "description": (
                        f"Return the {schema.__name__} result. Call this exactly once, "
                        "with every required field populated."
                    ),
                    "input_schema": json_schema_for(schema),
                }
            ],
            # `any` (use one of the supplied tools), not `{"type": "tool", "name":
            # ...}`. The named form is what Anthropic documents for forcing one
            # specific tool, and DeepSeek rejects it outright with
            # `400: "Thinking mode does not support this tool_choice"` -- flash runs
            # thinking by default. Probed against the live endpoint 2026-09-13:
            # `any`, `auto` and no tool_choice all succeed; only the named form 400s.
            #
            # Exactly one tool is offered, so `any` forces it just as precisely.
            "tool_choice": {"type": "any"},
        }

        started = time.perf_counter()
        try:
            response = await self._request(kwargs, stream=max_tokens > _STREAM_THRESHOLD)
        except anthropic.APIStatusError as exc:
            raise _classify_status_error(self.name, exc) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(self.name, f"deepseek connection error: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderRefusal(
                self.name,
                getattr(details, "category", None),
                f"deepseek declined the request ({getattr(details, 'category', 'unspecified')})",
                usage=self._usage(response, elapsed_ms),
            )

        for block in response.content:
            is_result = getattr(block, "type", None) == "tool_use" and (
                getattr(block, "name", None) == tool_name
            )
            if is_result:
                try:
                    return Completion(
                        value=schema.model_validate(block.input),
                        provider=self.name,
                        model=str(getattr(response, "model", model)),
                        usage=self._usage(response, elapsed_ms),
                    )
                except ValidationError as exc:
                    raise SchemaViolation(
                        f"deepseek output failed {schema.__name__} validation: {exc}",
                        usage=self._usage(response, elapsed_ms),
                    ) from exc

        # Forced tool choice means this should be unreachable. If it happens, the
        # endpoint has stopped honouring `tool_choice` too, and the useful thing is
        # to say so plainly rather than to guess at the prose that came back.
        block_types = [getattr(block, "type", "?") for block in response.content]
        raise SchemaViolation(
            f"deepseek ignored a forced tool_choice for {schema.__name__} "
            f"(stop_reason={getattr(response, 'stop_reason', None)!r}, "
            f"content_block_types={block_types})",
            usage=self._usage(response, elapsed_ms),
        )
