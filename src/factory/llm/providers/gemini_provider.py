"""Gemini provider — orchestration, discovery, compliance, marketing copy.

Gemini safety blocks are mapped onto the same ``ProviderRefusal`` the Claude path
raises, so retry and escalation logic upstream never has to know which vendor
answered.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from factory.llm.providers.base import (
    AgentTurn,
    Completion,
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    SchemaViolation,
    ToolCall,
    Turn,
    Usage,
)
from factory.logging import get_logger
from factory.settings import Settings, TaskType, get_settings

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_BLOCKING_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _strip_additional_properties(node: Any) -> None:
    """Recursively delete JSON Schema keywords the Gemini Developer API's structured-
    output validator can't handle, in place.

    - ``additionalProperties``/``additional_properties``: not supported at all outside
      Vertex AI / Gemini Enterprise -- google-genai's own schema processor hard-raises
      a local ``ValueError`` if it sees the key rather than dropping it, and prior to
      that check even reaching it, this key was observed passing straight through
      unrenamed into the literal request body, which the live API then rejects with
      `Unknown name "additional_properties" ... Cannot find field.`. Pydantic's
      ``model_json_schema()`` sets this wherever a ``dict[str, ...]``-typed field
      appears, including inside a ``$defs`` sub-schema for a nested model. Every
      structured-output Gemini call failed until this was stripped.
    - ``maxItems``/``minItems``: triggers a bare `400 INVALID_ARGUMENT: Request
      contains an invalid argument` -- with no further detail in the response body --
      whenever the array's items schema (reached via a ``$ref``'d nested model) is
      complex enough. Reproduced directly against the live API by bisection: an
      array capped with ``maxItems`` of a one-field item works; the identical cap on
      an array of an 11-field item (mixing string-length, ``format: uri``, and
      numeric-range constraints -- i.e. exactly ``DiscriminatedIdeas.ideas: list
      [ScoredIdea]``) does not, regardless of which specific fields are on the item.
      Safe to drop: Pydantic's own ``model_validate_json()`` re-enforces the same
      ``max_length``/``min_length`` bound locally once the response comes back, so
      nothing downstream ever relied on Gemini enforcing it server-side.

    Neither of these was ever exercised against the real API before either bug
    surfaced: every existing test mocks the client.
    """
    if isinstance(node, dict):
        node.pop("additionalProperties", None)
        node.pop("additional_properties", None)
        node.pop("maxItems", None)
        node.pop("minItems", None)
        for value in node.values():
            _strip_additional_properties(value)
    elif isinstance(node, list):
        for item in node:
            _strip_additional_properties(item)


def _sanitize_tools_for_gemini(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip unsupported JSON Schema keywords from each tool's parameter schema for
    Gemini (see ``_strip_additional_properties``).

    Deep-copies first: each tool dict's ``"parameters"`` is the *same shared dict
    object* as ``ToolDescriptor.input_schema`` (``registry.py``'s ``to_gemini()``
    passes it by reference, no copy), which ``to_anthropic()`` also reads for
    strict-mode tool schemas. Mutating in place would silently corrupt what
    Anthropic sees on its next call using the same descriptor.
    """
    sanitized_tools = []
    for tool in tools:
        sanitized = dict(tool)
        params = sanitized.get("parameters")
        if isinstance(params, dict):
            params = copy.deepcopy(params)
            _strip_additional_properties(params)
            sanitized["parameters"] = params
        sanitized_tools.append(sanitized)
    return sanitized_tools


def _billable_input_tokens(meta: Any) -> int:
    """Gemini's ``prompt_token_count`` is *inclusive* of cached tokens, unlike
    Anthropic's ``input_tokens`` which is exclusive. Normalize here, at the one
    place that knows which convention this vendor uses, so everything downstream
    (cost accounting, metering) can assume the Anthropic convention unconditionally."""
    prompt_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
    cache_read_tokens = int(getattr(meta, "cached_content_token_count", 0) or 0)
    return max(prompt_tokens - cache_read_tokens, 0)


def _to_gemini_content(turn: Turn) -> dict[str, Any]:
    """Render a neutral turn as Gemini content parts.

    Gemini calls the assistant role "model" and returns tool output as a
    ``function_response`` part on a *user* turn.
    """
    if turn.tool_results:
        return {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": result.name,
                        "response": {"result": result.content, "is_error": result.is_error},
                    }
                }
                for result in turn.tool_results
            ],
        }

    parts: list[dict[str, Any]] = []
    if turn.text:
        parts.append({"text": turn.text})
    for call in turn.tool_calls:
        parts.append({"function_call": {"name": call.name, "args": call.arguments}})
    return {
        "role": "model" if turn.role == "assistant" else "user",
        "parts": parts or [{"text": "(no content)"}],
    }


class GeminiProvider:
    name = Provider.GEMINI

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: genai.Client | None = None

    @property
    def available(self) -> bool:
        return self._settings.has_gemini

    def _get_client(self) -> genai.Client:
        if self._client is None:
            key = self._settings.secret("GEMINI_API_KEY")
            if not key:
                raise ProviderUnavailable(self.name, "GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=key)
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
        client = self._get_client()
        # No explicit prompt-caching API is used here (a separate, larger surface);
        # simply prepending keeps behaviour identical for every existing caller,
        # which all pass `None`, and still lets Gemini's implicit caching pick up the
        # repeated prefix server-side for free if it chooses to.
        instruction = f"{cache_prefix}\n\n{system}" if cache_prefix else system
        schema_dict = schema.model_json_schema()
        _strip_additional_properties(schema_dict)
        config_kwargs: dict[str, Any] = {
            "system_instruction": instruction,
            "response_mime_type": "application/json",
            "response_schema": schema_dict,
            "max_output_tokens": max_tokens,
        }
        if tools:
            config_kwargs["tools"] = [{"function_declarations": _sanitize_tools_for_gemini(tools)}]

        started = time.perf_counter()
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", None)
            if status in _RETRYABLE_STATUS or status is None:
                raise ProviderUnavailable(self.name, f"gemini error: {exc}", status=status) from exc
            raise ProviderUnavailable(self.name, f"gemini error: {exc}", status=status) from exc
        except (OSError, TimeoutError) as exc:
            raise ProviderUnavailable(self.name, f"gemini transport error: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._raise_if_blocked(response)

        text = getattr(response, "text", None) or ""
        if not text.strip():
            raise SchemaViolation("gemini returned no text content")

        try:
            value = schema.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise SchemaViolation(
                f"gemini output failed {schema.__name__} validation: {exc}"
            ) from exc

        meta = getattr(response, "usage_metadata", None)
        return Completion(
            value=value,
            provider=self.name,
            model=model,
            usage=Usage(
                input_tokens=_billable_input_tokens(meta),
                output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
                cache_read_tokens=int(getattr(meta, "cached_content_token_count", 0) or 0),
                latency_ms=elapsed_ms,
            ),
        )

    def _raise_if_blocked(self, response: Any, *, allow_max_tokens: bool = False) -> None:
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            raise ProviderRefusal(
                self.name, str(block_reason), f"gemini blocked the prompt ({block_reason})"
            )

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise ProviderRefusal(self.name, None, "gemini returned no candidates")

        finish_reason = str(getattr(candidates[0], "finish_reason", "") or "").upper()
        reason_name = finish_reason.rsplit(".", 1)[-1]
        if reason_name in _BLOCKING_FINISH_REASONS:
            raise ProviderRefusal(self.name, reason_name, f"gemini stopped on {reason_name}")
        if reason_name == "MAX_TOKENS" and not allow_max_tokens:
            # Fatal for structured output (truncated JSON is unparseable) but merely a
            # short turn in a conversation, which the agent loop can continue from.
            raise SchemaViolation(
                "gemini hit max_output_tokens before completing the JSON object; "
                "raise max_tokens for this call"
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
        client = self._get_client()
        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        if tools:
            config_kwargs["tools"] = [{"function_declarations": _sanitize_tools_for_gemini(tools)}]

        started = time.perf_counter()
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=[_to_gemini_content(turn) for turn in turns],
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
        except genai_errors.APIError as exc:
            raise ProviderUnavailable(
                self.name, f"gemini error: {exc}", status=getattr(exc, "code", None)
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ProviderUnavailable(self.name, f"gemini transport error: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._raise_if_blocked(response, allow_max_tokens=True)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        candidates = getattr(response, "candidates", None) or []
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        for index, part in enumerate(parts):
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                calls.append(
                    ToolCall(
                        # Gemini does not mint call ids, so one is synthesised. It only
                        # has to round-trip within this turn.
                        id=f"call_{index}_{getattr(function_call, 'name', 'tool')}",
                        name=str(getattr(function_call, "name", "")),
                        arguments=dict(getattr(function_call, "args", None) or {}),
                    )
                )
            elif getattr(part, "text", None):
                text_parts.append(str(part.text))

        meta = getattr(response, "usage_metadata", None)
        return AgentTurn(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=str(getattr(candidates[0], "finish_reason", "") or ""),
            provider=self.name,
            model=model,
            usage=Usage(
                input_tokens=_billable_input_tokens(meta),
                output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
                cache_read_tokens=int(getattr(meta, "cached_content_token_count", 0) or 0),
                latency_ms=elapsed_ms,
            ),
        )

    async def list_models(self) -> list[str]:
        try:
            pager = await self._get_client().aio.models.list()
        except Exception as exc:
            raise ProviderUnavailable(self.name, f"could not list models: {exc}") from exc

        names: list[str] = []
        async for model in pager:
            raw = getattr(model, "name", "") or ""
            names.append(raw.removeprefix("models/"))
        return names


__all__ = ["GeminiProvider"]
