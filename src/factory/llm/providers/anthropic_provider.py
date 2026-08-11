"""Claude provider — code generation and the Bug Council.

Follows the current Messages API contract for Opus 5: adaptive thinking is on by
default (so no ``thinking`` block is sent), sampling parameters are not accepted,
assistant prefill is not supported, and a refusal arrives as a successful HTTP 200
with ``stop_reason == "refusal"`` — which is why ``stop_reason`` is checked before
``content`` is ever indexed.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from factory.llm.providers.base import (
    AgentTurn,
    Completion,
    LLMError,
    Provider,
    ProviderRefusal,
    ProviderRequestRejected,
    ProviderUnavailable,
    SchemaViolation,
    ToolCall,
    Turn,
    Usage,
)
from factory.llm.schema import json_schema_for
from factory.logging import get_logger
from factory.settings import Settings, TaskType, get_settings

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# Opting into server-side fallbacks means a policy decline on a benign request is
# re-served by another model inside the same call instead of losing the work.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Above this, a non-streaming request risks an HTTP timeout before the model finishes.
_STREAM_THRESHOLD = 16_000

#: Permanent failures -- retrying the identical request against Anthropic again will
#: never succeed. A different vendor may still accept it (e.g. a schema keyword one
#: provider's dialect rejects), so these still raise `ProviderRequestRejected`
#: (failover-eligible), not a plain error -- only the same-provider retry loop is
#: skipped.
_PERMANENT_STATUS = {400, 401, 403, 404, 422}


def _classify_status_error(
    name: Provider, exc: anthropic.APIStatusError
) -> ProviderUnavailable:
    detail = f"anthropic {exc.status_code}: {exc}"
    if exc.status_code in _PERMANENT_STATUS:
        return ProviderRequestRejected(name, detail, status=exc.status_code)
    return ProviderUnavailable(name, detail, status=exc.status_code)


def _system_blocks(system: str, cache_prefix: str | None) -> list[dict[str, Any]]:
    """Without a shared prefix: one block, the whole system prompt cached. With one:
    the prefix comes first and is the only block marked cacheable, and the per-call
    system prompt comes second, uncached — a tiny distinct block cached per caller
    would each pay the write premium for zero reads.

    Anthropic's cache-prefix order is tools -> system -> messages, so putting the
    varying content *after* the shared block is what lets several calls that share a
    prefix but differ in `system` all hit the same cache entry.
    """
    if cache_prefix:
        return [
            {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system},
        ]
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _to_anthropic_message(turn: Turn) -> dict[str, Any]:
    """Render a neutral turn as Anthropic content blocks.

    Tool results are a *user* turn in this API even though they are machine output,
    which is the main reason the neutral `Turn` shape exists.
    """
    if turn.tool_results:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in turn.tool_results
            ],
        }

    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return {"role": turn.role, "content": blocks or [{"type": "text", "text": "(no content)"}]}


class AnthropicProvider:
    name = Provider.ANTHROPIC

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: anthropic.AsyncAnthropic | None = None
        # Flipped off permanently if the installed SDK or account rejects the beta,
        # so one unsupported feature does not cost a retry on every later call.
        self._use_fallbacks = True

    @property
    def available(self) -> bool:
        return self._settings.has_anthropic

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = self._settings.secret("ANTHROPIC_API_KEY")
            if not key:
                raise ProviderUnavailable(self.name, "ANTHROPIC_API_KEY is not set")
            self._client = anthropic.AsyncAnthropic(api_key=key)
        return self._client

    def _effort(self, task_type: TaskType) -> str:
        if task_type is TaskType.CODE_GENERATION:
            return self._settings.effort_code
        if task_type is TaskType.CODE_REVIEW:
            return self._settings.effort_review
        return "high"

    def _usage(self, response: Any, elapsed_ms: int) -> Usage:
        """Real usage from a response that still failed downstream (refusal, schema
        violation) -- these are frequently billed, successful HTTP 200s, most
        notably a refusal (`stop_reason == "refusal"` arrives as a normal 200) and a
        thinking-budget-exhausted empty answer (a full paid generation)."""
        usage = getattr(response, "usage", None)
        return Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            latency_ms=elapsed_ms,
        )

    async def _request(self, kwargs: dict[str, Any], *, stream: bool) -> Any:
        client = self._get_client()
        if self._use_fallbacks:
            beta_kwargs = dict(kwargs, betas=[_FALLBACK_BETA], fallbacks="default")
            try:
                if stream:
                    async with client.beta.messages.stream(**beta_kwargs) as s:
                        return await s.get_final_message()
                return await client.beta.messages.create(**beta_kwargs)
            except TypeError as exc:
                # Can only mean the installed SDK does not recognise betas=/fallbacks=
                # at all (there is no other reason this call would raise TypeError).
                # Degrade once, loudly, and never pay the failed attempt again.
                self._use_fallbacks = False
                log.warning("anthropic.fallbacks_unavailable", detail=str(exc)[:300])
            except anthropic.BadRequestError as exc:
                detail = str(exc)
                if "beta" not in detail.lower() and "fallback" not in detail.lower():
                    # A genuine request error (bad schema, bad tool definition, an
                    # unsupported `effort` value, ...) unrelated to the beta header
                    # itself -- must not be silently retried without the beta (which
                    # just repeats the same failure with a different symptom) and
                    # must not permanently disable fallbacks for the rest of the
                    # process over an unrelated one-off error. Let it propagate to
                    # parse()/converse()'s own APIStatusError handler, which
                    # classifies it correctly (permanent vs. retryable) by status.
                    raise
                self._use_fallbacks = False
                log.warning("anthropic.fallbacks_unavailable", detail=detail[:300])

        if stream:
            async with client.messages.stream(**kwargs) as s:
                return await s.get_final_message()
        return await client.messages.create(**kwargs)

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": _system_blocks(system, cache_prefix),
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "format": {"type": "json_schema", "schema": json_schema_for(schema)},
                "effort": self._effort(task_type),
            },
        }
        if tools:
            kwargs["tools"] = tools

        started = time.perf_counter()
        try:
            response = await self._request(kwargs, stream=max_tokens > _STREAM_THRESHOLD)
        except anthropic.APIStatusError as exc:
            raise _classify_status_error(self.name, exc) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(self.name, f"anthropic connection error: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # Check the stop reason before touching content: on a refusal, content is
        # empty or partial and indexing it would raise the wrong error.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderRefusal(
                self.name,
                getattr(details, "category", None),
                f"claude declined the request ({getattr(details, 'category', 'unspecified')})",
                usage=self._usage(response, elapsed_ms),
            )

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            # Deliberately over-informative: this fires rarely enough (and burns a full
            # paid call each time) that guessing the cause from a bare message wastes a
            # whole retry cycle. The likely culprit is adaptive thinking consuming the
            # entire max_tokens budget before any answer text is produced -- stop_reason
            # "max_tokens" with only a "thinking" block confirms that; anything else
            # points elsewhere.
            block_types = [getattr(block, "type", "?") for block in response.content]
            raise SchemaViolation(
                "claude returned no text content "
                f"(stop_reason={getattr(response, 'stop_reason', None)!r}, "
                f"content_block_types={block_types}, max_tokens={max_tokens}, "
                f"usage={getattr(response, 'usage', None)!r})",
                usage=self._usage(response, elapsed_ms),
            )

        try:
            value = schema.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise SchemaViolation(
                f"claude output failed {schema.__name__} validation: {exc}",
                usage=self._usage(response, elapsed_ms),
            ) from exc

        return Completion(
            value=value,
            provider=self.name,
            model=str(getattr(response, "model", model)),
            usage=self._usage(response, elapsed_ms),
        )

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [_to_anthropic_message(turn) for turn in turns],
            "output_config": {"effort": self._effort(task_type)},
        }
        if tools:
            kwargs["tools"] = tools

        started = time.perf_counter()
        try:
            response = await self._request(kwargs, stream=max_tokens > _STREAM_THRESHOLD)
        except anthropic.APIStatusError as exc:
            raise _classify_status_error(self.name, exc) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(self.name, f"anthropic connection error: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderRefusal(
                self.name,
                getattr(details, "category", None),
                "claude declined the request",
                usage=self._usage(response, elapsed_ms),
            )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.id),
                        name=str(block.name),
                        arguments=dict(block.input or {}),
                    )
                )

        return AgentTurn(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=str(getattr(response, "stop_reason", "") or ""),
            provider=self.name,
            model=str(getattr(response, "model", model)),
            usage=self._usage(response, elapsed_ms),
        )

    async def list_models(self) -> list[str]:
        try:
            page = await self._get_client().models.list()
        except Exception as exc:
            raise ProviderUnavailable(self.name, f"could not list models: {exc}") from exc
        return [m.id for m in page.data]


__all__ = ["AnthropicProvider", "LLMError"]
