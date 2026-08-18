"""The single entry point every engine uses to talk to a model.

Engines call ``client.parse(Schema, task_type=...)`` and get a validated contract
back. Provider choice, retries, failover, refusal semantics, and cost accounting all
live here so no engine has to reason about them.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from factory.llm.providers.base import (
    AgentTurn,
    LLMProvider,
    Provider,
    ProviderRefusal,
    ProviderRequestRejected,
    ProviderUnavailable,
    SchemaViolation,
    Turn,
    Usage,
)
from factory.llm.router import (
    ProviderRegistry,
    max_tokens_for,
    may_failover,
    model_for,
    provider_for,
)
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.tools.registry import ToolDescriptor, anthropic_payload, gemini_payload

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_MAX_TRANSPORT_ATTEMPTS = 3
_MAX_SCHEMA_REPAIRS = 1


@dataclass(slots=True)
class UsageRecord:
    provider: str
    model: str
    task_type: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    outcome: str
    engine: str | None = None
    task_id: str | None = None
    tunable_parameter_id: str | None = None


UsageRecorder = Callable[[UsageRecord], Awaitable[None]]


class NoProviderAvailable(RuntimeError):
    """Neither the routed provider nor any permitted alternative can serve this call."""


def _tools_payload(
    descriptors: Sequence[ToolDescriptor], provider: Provider
) -> list[dict[str, Any]]:
    """Only the serving provider's shape is correct; rendering happens per attempt,
    not once up front, so a failover to the other vendor never sends the wrong one.

    DeepSeek shares Anthropic's shape deliberately, not by omission: it is served
    over DeepSeekProvider(AnthropicProvider), the same Anthropic-compatible
    endpoint/message format -- sending it Gemini's tool-schema shape produced a
    real, reproducible 400 ("Invalid schema for function ...: null is not of
    types 'boolean', 'object'") the first time CODE_REPAIR actually ran against
    production traffic, caught before this shipped to real users.
    """
    if provider in (Provider.ANTHROPIC, Provider.DEEPSEEK):
        return anthropic_payload(descriptors)
    return gemini_payload(descriptors)


class LLMClient:
    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        settings: Settings | None = None,
        usage_recorder: UsageRecorder | None = None,
        backoff_base: float = 2.0,
    ) -> None:
        self._registry = registry
        self._settings = settings or get_settings()
        self._record = usage_recorder
        # Tests set this to 0 so the retry path is exercised without real sleeps.
        self._backoff_base = backoff_base

    @property
    def registry(self) -> ProviderRegistry:
        """Exposed so the agent runtime can reach providers for tool-calling turns."""
        return self._registry

    async def parse(
        self,
        schema: type[SchemaT],
        *,
        task_type: TaskType,
        system: str,
        prompt: str,
        engine: EngineName | None = None,
        task_id: str | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        cache_prefix: str | None = None,
        tunable_parameter_id: str | None = None,
    ) -> SchemaT:
        primary = provider_for(task_type)
        candidates: list[LLMProvider] = []

        routed = self._registry.get(primary)
        if routed is not None:
            candidates.append(routed)
        if may_failover(task_type):
            candidates.extend(self._registry.alternatives(exclude=primary))

        if not candidates:
            raise NoProviderAvailable(
                f"no available provider for {task_type} (routed to {primary})"
            )

        last_error: Exception | None = None
        for index, provider in enumerate(candidates):
            if index > 0:
                log.warning(
                    "llm.failover",
                    task_type=str(task_type),
                    from_provider=str(primary),
                    to_provider=str(provider.name),
                )
            try:
                return await self._attempt_provider(
                    provider,
                    schema,
                    task_type=task_type,
                    system=system,
                    prompt=prompt,
                    engine=engine,
                    task_id=task_id,
                    max_tokens=max_tokens,
                    tools=tools,
                    cache_prefix=cache_prefix,
                    tunable_parameter_id=tunable_parameter_id,
                )
            except ProviderRefusal:
                # A policy decline is a decision, not an outage. Trying the other
                # vendor to get a different answer is exactly the wrong response.
                raise
            except ProviderUnavailable as exc:
                last_error = exc
                continue

        assert last_error is not None
        raise last_error

    async def _attempt_provider(
        self,
        provider: LLMProvider,
        schema: type[SchemaT],
        *,
        task_type: TaskType,
        system: str,
        prompt: str,
        engine: EngineName | None,
        task_id: str | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        cache_prefix: str | None = None,
        tunable_parameter_id: str | None = None,
    ) -> SchemaT:
        model = self._model_for_provider(provider.name, task_type)
        budget = max_tokens or max_tokens_for(task_type)
        effective_prompt = prompt
        repairs = 0
        transport_attempts = 0

        while True:
            try:
                completion = await provider.parse(
                    schema,
                    system=system,
                    prompt=effective_prompt,
                    task_type=task_type,
                    model=model,
                    max_tokens=budget,
                    tools=tools,
                    cache_prefix=cache_prefix,
                )
            except ProviderRequestRejected:
                # Permanent: the identical request will never succeed against this
                # provider, so retrying it with backoff would just waste latency.
                # Still recorded and re-raised immediately -- the outer parse()/
                # converse() loop treats this as a ProviderUnavailable subclass and
                # moves straight to the next candidate provider (if any).
                await self._emit(
                    provider,
                    model,
                    task_type,
                    engine,
                    task_id,
                    outcome="rejected",
                    tunable_parameter_id=tunable_parameter_id,
                )
                raise
            except ProviderUnavailable as exc:
                transport_attempts += 1
                if transport_attempts >= _MAX_TRANSPORT_ATTEMPTS:
                    await self._emit(
                        provider,
                        model,
                        task_type,
                        engine,
                        task_id,
                        outcome="unavailable",
                        tunable_parameter_id=tunable_parameter_id,
                    )
                    raise
                delay = min(self._backoff_base**transport_attempts, 30.0)
                delay += random.uniform(0, delay * 0.25)  # noqa: S311 - jitter, not crypto
                log.warning(
                    "llm.retry",
                    provider=str(provider.name),
                    attempt=transport_attempts,
                    delay_s=round(delay, 2),
                    detail=str(exc)[:200],
                )
                await asyncio.sleep(delay)
                continue
            except ProviderRefusal as exc:
                await self._emit(
                    provider,
                    model,
                    task_type,
                    engine,
                    task_id,
                    outcome="refusal",
                    usage=exc.usage,
                    tunable_parameter_id=tunable_parameter_id,
                )
                raise
            except SchemaViolation as exc:
                repairs += 1
                if repairs > _MAX_SCHEMA_REPAIRS:
                    await self._emit(
                        provider,
                        model,
                        task_type,
                        engine,
                        task_id,
                        outcome="schema_violation",
                        usage=exc.usage,
                        tunable_parameter_id=tunable_parameter_id,
                    )
                    raise
                # One corrective pass: show the model exactly what failed rather than
                # replaying the identical prompt and hoping. The failed attempt was a
                # real, billed call (often a full generation that just didn't parse),
                # so it is recorded here even though it isn't the terminal outcome --
                # previously only a repair that *also* failed got recorded at all.
                await self._emit(
                    provider,
                    model,
                    task_type,
                    engine,
                    task_id,
                    outcome="schema_violation_repaired",
                    usage=exc.usage,
                    tunable_parameter_id=tunable_parameter_id,
                )
                effective_prompt = (
                    f"{prompt}\n\n---\nYour previous response was rejected by schema "
                    f"validation:\n{exc}\nReturn only a JSON object that satisfies the "
                    f"{schema.__name__} schema."
                )
                log.warning("llm.schema_repair", schema=schema.__name__, detail=str(exc)[:200])
                continue

            await self._emit(
                provider,
                model,
                task_type,
                engine,
                task_id,
                outcome="ok",
                usage=completion.usage,
                tunable_parameter_id=tunable_parameter_id,
            )
            return completion.value

    async def converse(
        self,
        *,
        system: str,
        turns: list[Turn],
        tool_descriptors: Sequence[ToolDescriptor],
        task_type: TaskType,
        engine: EngineName | None = None,
        task_id: str | None = None,
        max_tokens: int | None = None,
    ) -> AgentTurn:
        """One agent tool-calling step, with the same failover and cost accounting
        as ``parse``. Agent runs previously called providers directly and bypassed
        both, so no token they spent was ever recorded.

        Tools are rendered into the serving provider's own payload shape *inside*
        this call, not by the caller, so a mid-run failover to a different vendor
        never sends the wrong shape.
        """
        primary = provider_for(task_type)
        candidates: list[LLMProvider] = []

        routed = self._registry.get(primary)
        if routed is not None:
            candidates.append(routed)
        if may_failover(task_type):
            candidates.extend(self._registry.alternatives(exclude=primary))

        if not candidates:
            raise NoProviderAvailable(
                f"no available provider for {task_type} (routed to {primary})"
            )

        last_error: Exception | None = None
        for index, provider in enumerate(candidates):
            if index > 0:
                log.warning(
                    "llm.failover",
                    task_type=str(task_type),
                    from_provider=str(primary),
                    to_provider=str(provider.name),
                )
            try:
                return await self._attempt_provider_converse(
                    provider,
                    task_type=task_type,
                    system=system,
                    turns=turns,
                    tool_descriptors=tool_descriptors,
                    engine=engine,
                    task_id=task_id,
                    max_tokens=max_tokens,
                )
            except ProviderRefusal:
                raise
            except ProviderUnavailable as exc:
                last_error = exc
                continue

        assert last_error is not None
        raise last_error

    async def _attempt_provider_converse(
        self,
        provider: LLMProvider,
        *,
        task_type: TaskType,
        system: str,
        turns: list[Turn],
        tool_descriptors: Sequence[ToolDescriptor],
        engine: EngineName | None,
        task_id: str | None,
        max_tokens: int | None,
    ) -> AgentTurn:
        model = self._model_for_provider(provider.name, task_type)
        budget = max_tokens or max_tokens_for(task_type)
        tools = _tools_payload(tool_descriptors, provider.name)
        transport_attempts = 0

        while True:
            try:
                turn = await provider.converse(
                    system=system,
                    turns=turns,
                    tools=tools,
                    task_type=task_type,
                    model=model,
                    max_tokens=budget,
                )
            except ProviderRequestRejected:
                await self._emit(provider, model, task_type, engine, task_id, outcome="rejected")
                raise
            except ProviderUnavailable as exc:
                transport_attempts += 1
                if transport_attempts >= _MAX_TRANSPORT_ATTEMPTS:
                    await self._emit(
                        provider, model, task_type, engine, task_id, outcome="unavailable"
                    )
                    raise
                delay = min(self._backoff_base**transport_attempts, 30.0)
                delay += random.uniform(0, delay * 0.25)  # noqa: S311 - jitter, not crypto
                log.warning(
                    "llm.retry",
                    provider=str(provider.name),
                    attempt=transport_attempts,
                    delay_s=round(delay, 2),
                    detail=str(exc)[:200],
                )
                await asyncio.sleep(delay)
                continue
            except ProviderRefusal as exc:
                await self._emit(
                    provider,
                    model,
                    task_type,
                    engine,
                    task_id,
                    outcome="refusal",
                    usage=exc.usage,
                )
                raise

            await self._emit(
                provider,
                model,
                task_type,
                engine,
                task_id,
                outcome="ok",
                usage=turn.usage,
            )
            return turn

    def _model_for_provider(self, provider: Provider, task_type: TaskType) -> str:
        """Failover changes the vendor, so the model id has to change with it.

        ``model_code`` (Opus 5) is deliberately not used here: CODE_GENERATION/
        CODE_REVIEW are in `router.NO_FAILOVER` and never reach this branch, so a
        failover here is always some normally-cheap Gemini-routed task (orchestration,
        discovery, analysis, compliance, marketing copy) -- using the code-tier model
        would silently run every one of them at ~10-25x the intended price for as
        long as the primary provider stays down.
        """
        if provider is provider_for(task_type):
            return model_for(task_type, self._settings)
        if provider is Provider.ANTHROPIC:
            return self._settings.model_fallback_anthropic
        return self._settings.model_general

    async def _emit(
        self,
        provider: LLMProvider,
        model: str,
        task_type: TaskType,
        engine: EngineName | None,
        task_id: str | None,
        *,
        outcome: str,
        usage: Usage | None = None,
        tunable_parameter_id: str | None = None,
    ) -> None:
        record = UsageRecord(
            provider=str(provider.name),
            model=model,
            task_type=str(task_type),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            latency_ms=usage.latency_ms if usage else 0,
            outcome=outcome,
            engine=str(engine) if engine else None,
            task_id=task_id,
            tunable_parameter_id=tunable_parameter_id,
        )
        log.info(
            "llm.call",
            provider=record.provider,
            model=record.model,
            task_type=record.task_type,
            outcome=record.outcome,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            latency_ms=record.latency_ms,
        )
        if self._record is not None:
            await self._record(record)


def build_registry(settings: Settings | None = None) -> ProviderRegistry:
    """Construct the real providers. Unconfigured ones report themselves unavailable.

    ``staging_mock_expensive_calls`` swaps only the Anthropic slot for a
    deterministic fixture provider (see ``llm/providers/staging.py``) -- Gemini and
    DeepSeek are never touched by this flag (DeepSeek serves CODE_REPAIR, which is
    already the cheap option this flag exists to avoid paying for), and it is
    orthogonal to ``factory_dry_run``.
    """
    from factory.llm.providers.anthropic_provider import AnthropicProvider
    from factory.llm.providers.deepseek_provider import DeepSeekProvider
    from factory.llm.providers.gemini_provider import GeminiProvider

    settings = settings or get_settings()
    anthropic: LLMProvider
    if settings.staging_mock_expensive_calls:
        from factory.llm.providers.staging import StagingCodeProvider

        anthropic = StagingCodeProvider()
    else:
        anthropic = AnthropicProvider(settings)
    return ProviderRegistry(
        {
            Provider.DEEPSEEK: DeepSeekProvider(settings),
            Provider.GEMINI: GeminiProvider(settings),
            Provider.ANTHROPIC: anthropic,
        }
    )


def build_client(
    settings: Settings | None = None,
    *,
    usage_recorder: UsageRecorder | None = None,
) -> LLMClient:
    settings = settings or get_settings()
    return LLMClient(build_registry(settings), settings=settings, usage_recorder=usage_recorder)
