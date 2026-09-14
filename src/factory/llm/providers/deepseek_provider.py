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

import copy
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
    Image,
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    SchemaViolation,
)
from factory.llm.schema import json_schema_for
from factory.logging import get_logger
from factory.settings import TaskType

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_BASE_URL = "https://api.deepseek.com/anthropic"


def _validate_tolerating_extras[T: BaseModel](schema: type[T], payload: Any) -> T:
    """Validate `payload`, salvaging the one failure mode DeepSeek actually causes.

    `Contract` sets `extra="forbid"` deliberately -- a field the schema never
    defined is usually a sign the model misunderstood the task, and catching that is
    worth having. But DeepSeek does not enforce `additionalProperties: false` during
    tool-call decoding (the schema says it; the endpoint treats it as advice), and
    the result is real work destroyed by noise: one reviewer attached an empty
    `line_placeholder` to each finding and the whole Bug Council review was rejected,
    escalating a patch that had nothing wrong with it.

    So exactly one rescue is allowed: if *every* error is `extra_forbidden`, drop
    precisely those keys and revalidate. Any other error -- a missing required
    field, a bad enum, a constraint violation -- still fails, because those are the
    ones that mean the model got the task wrong rather than the shape.

    Dropped keys are logged, never silently discarded: if a model starts inventing
    fields regularly that is a signal about the prompt or the schema, not something
    to bury.
    """
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        if not errors or not all(e.get("type") == "extra_forbidden" for e in errors):
            raise
        pruned = copy.deepcopy(payload)
        dropped: list[str] = []
        for error in errors:
            location = error.get("loc", ())
            if not location:
                continue
            node: Any = pruned
            try:
                for key in location[:-1]:
                    node = node[key]
                del node[location[-1]]
            except (KeyError, IndexError, TypeError):
                # The path did not survive an earlier deletion, or is not indexable.
                # Revalidation below decides the outcome either way.
                continue
            dropped.append(".".join(str(part) for part in location))
        value = schema.model_validate(pruned)
        log.warning("deepseek.dropped_extra_fields", schema=schema.__name__, fields=dropped)
        return value


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
        # Accepted and ignored. A provider without vision answering from the text
        # alone is the right degradation -- failing the call would turn "this model
        # cannot see" into an outage. Callers that genuinely need eyes check
        # `vision_capable` rather than assuming.
        images: list[Image] | None = None,
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
            # Thinking and the answer share `max_tokens`, and on a large input
            # thinking will take all of it. Observed live on a Bug Council review,
            # whose corpus is an entire codebase: `stop_reason='max_tokens'` with a
            # single `thinking` block and no `tool_use` at all, so the review was
            # lost. `max_tokens_for(CODE_REVIEW)` is 16_000 and thinking spent it.
            #
            # This is the same failure `max_tokens_for`'s CODE_GENERATION comment
            # describes on Claude Sonnet 5, where the only available lever was a
            # bigger ceiling because `budget_tokens` is rejected outright there.
            # DeepSeek accepts it -- verified against the live endpoint -- so the
            # cause can be fixed directly instead of out-spent: cap thinking so the
            # answer is always guaranteed room.
            #
            # Half, with the API's 1024 floor. Generous enough that reasoning quality
            # is not the thing being traded away, and it makes the failure
            # impossible rather than less likely.
            "thinking": {"type": "enabled", "budget_tokens": max(1024, max_tokens // 2)},
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

        # Shared with the base class, because a null `content` on a 200 is not a
        # DeepSeek quirk -- it is a hole every response-reading path had.
        blocks = self._content_blocks(response, elapsed_ms)

        for block in blocks:
            is_result = getattr(block, "type", None) == "tool_use" and (
                getattr(block, "name", None) == tool_name
            )
            if is_result:
                try:
                    value = _validate_tolerating_extras(schema, block.input)
                except ValidationError as exc:
                    raise SchemaViolation(
                        f"deepseek output failed {schema.__name__} validation: {exc}",
                        usage=self._usage(response, elapsed_ms),
                    ) from exc
                return Completion(
                    value=value,
                    provider=self.name,
                    model=str(getattr(response, "model", model)),
                    usage=self._usage(response, elapsed_ms),
                )

        block_types = [getattr(block, "type", "?") for block in blocks]
        stop_reason = getattr(response, "stop_reason", None)

        # Two very different causes, and conflating them sent me hunting the wrong
        # one: this message used to read "ignored a forced tool_choice" for a
        # response that had simply run out of room mid-thought.
        if stop_reason == "max_tokens":
            raise SchemaViolation(
                f"deepseek ran out of tokens before emitting the {schema.__name__} "
                f"tool call (max_tokens={max_tokens}, content_block_types="
                f"{block_types}) -- thinking consumed the budget; raise max_tokens "
                "or lower the thinking budget for this task type",
                usage=self._usage(response, elapsed_ms),
            )
        raise SchemaViolation(
            f"deepseek ignored a forced tool_choice for {schema.__name__} "
            f"(stop_reason={stop_reason!r}, content_block_types={block_types})",
            usage=self._usage(response, elapsed_ms),
        )
